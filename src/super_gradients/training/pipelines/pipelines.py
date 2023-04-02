from abc import ABC, abstractmethod
from typing import List, Optional, Tuple, Any, Union
from contextlib import contextmanager

import numpy as np
import torch

from super_gradients.training.utils.load_image import load_images, ImageType
from super_gradients.training.utils.detection_utils import DetectionPostPredictionCallback
from super_gradients.training.models.sg_module import SgModule
from super_gradients.training.models.results import Results, DetectionResults
from super_gradients.training.transforms.processing import Processing, ComposeProcessing


@contextmanager
def eval_mode(model: SgModule) -> None:
    """Set a model in evaluation mode and deactivate gradient computation, undo at the end.

    :param model: The model to set in evaluation mode.
    """
    _starting_mode = model.training
    model.eval()
    with torch.no_grad():
        yield
    model.train(mode=_starting_mode)


class Pipeline(ABC):
    """An abstract base class representing a processing pipeline for a specific task.
    The pipeline includes loading images, preprocessing, prediction, and postprocessing.

    :param model:           The model used for making predictions.
    :param image_processor: A single image processor or a list of image processors for preprocessing and postprocessing the images.
    :param device:          The device on which the model will be run. Defaults to "cpu". Use "cuda" for GPU support.
    """

    def __init__(self, model: SgModule, image_processor: Union[Processing, List[Processing]], device: Optional[str] = "cpu"):
        super().__init__()
        self.model = model.to(device)
        self.device = device

        if isinstance(image_processor, list):
            image_processor = ComposeProcessing(image_processor)
        self.image_processor = image_processor

    @abstractmethod
    def __call__(self, images: Union[ImageType, List[ImageType]]) -> Results:
        """Apply the pipeline on images and return the result.

        :param images:  Single image or a list of images of supported types.
        :return         Results object containing the results of the prediction and the image.
        """
        pass

    def _run(self, images: Union[ImageType, List[ImageType]]) -> Tuple[List[np.ndarray], List[Any]]:
        """Run the pipeline and return (image, predictions). The pipeline is made of 4 steps:
        1. Load images - Loading the images into a list of numpy arrays.
        2. Preprocess - Encode the image in the shape/format expected by the model
        3. Predict - Run the model on the preprocessed image
        4. Postprocess - Decode the output of the model so that the predictions are in the shape/format of original image.

        :param images:  Single image or a list of images of supported types.
        :return:
            - List of numpy arrays representing images.
            - List of model predictions.
        """
        self.model = self.model.to(self.device)  # Make sure the model is on the correct device

        images = load_images(images)

        # Preprocess
        preprocessed_images, processing_metadatas = [], []
        for image in images:
            preprocessed_image, processing_metadata = self.image_processor.preprocess_image(image=image.copy())
            preprocessed_images.append(preprocessed_image)
            processing_metadatas.append(processing_metadata)

        # Predict
        with eval_mode(self.model):
            torch_inputs = torch.Tensor(np.array(preprocessed_images)).to(self.device)
            model_output = self.model(torch_inputs)
            torch_predictions = self._decode_model_output(model_output)

        # Postprocess
        predictions = []
        for torch_prediction, processing_metadata in zip(torch_predictions, processing_metadatas):
            prediction = torch_prediction.detach().cpu().numpy()
            prediction = self.image_processor.postprocess_predictions(predictions=prediction, metadata=processing_metadata)
            predictions.append(prediction)

        return images, predictions

    @abstractmethod
    def _decode_model_output(self, model_output: Union[List, Tuple, torch.Tensor]) -> List[torch.Tensor]:
        """Decode the model output, which in some case is in a different format to the prediction.

        :param model_output:    Direct output of the model, without any post-processing.
        :return:                Model predictions. This might
        """
        pass

    def save_video(self, source_video_path: str, output_video_path: str) -> None:
        from super_gradients.training.utils.videos import load_video, save_video

        video, fps = load_video(file_path=source_video_path)
        results = self.model.predict(video)
        frames = results.draw(video)
        save_video(output_path=output_video_path, frames=frames, fps=fps)
        print(f"Saved video to {output_video_path}")


class DetectionPipeline(Pipeline):
    """Pipeline specifically designed for object detection tasks.
    The pipeline includes loading images, preprocessing, prediction, and postprocessing.

    :param model:                       The object detection model (instance of SgModule) used for making predictions.
    :param class_names:                 List of class names corresponding to the model's output classes.
    :param post_prediction_callback:    Callback function to process raw predictions from the model.
    :param image_processor:             Single image processor or a list of image processors for preprocessing and postprocessing the images.
    :param device:                      The device on which the model will be run. Defaults to "cpu". Use "cuda" for GPU support.
    """

    def __init__(
        self,
        model: SgModule,
        class_names: List[str],
        post_prediction_callback: DetectionPostPredictionCallback,
        device: Optional[str] = "cpu",
        image_processor: Optional[Processing] = None,
    ):
        super().__init__(model=model, device=device, image_processor=image_processor)
        self.post_prediction_callback = post_prediction_callback
        self.class_names = class_names

    def __call__(self, images: Union[List[ImageType], ImageType]) -> DetectionResults:
        """Apply the pipeline on images and return the detection result.

        :param images:  Single image or a list of images of supported types.
        :return         Results object containing the results of the prediction and the image.
        """
        images, predictions = self._run(images=images)
        return DetectionResults(images=images, predictions=predictions, class_names=self.class_names)

    def _decode_model_output(self, model_output) -> List[torch.Tensor]:
        """Decode the model output, by applying post prediction callback. This includes NMS.

        :param model_output:    Direct output of the model, without any post-processing.
        :return:                Predicted Bboxes.
        """
        decoded_predictions = self.post_prediction_callback(model_output, device=self.device)
        decoded_predictions = [
            decoded_prediction if decoded_prediction is not None else torch.zeros((0, 6), dtype=torch.float32) for decoded_prediction in decoded_predictions
        ]
        return decoded_predictions
