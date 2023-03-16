import math
from typing import Tuple, Union

import torch
from omegaconf import DictConfig
from torch import nn, Tensor

import super_gradients.common.factories.detection_modules_factory as det_factory
from super_gradients.common.registry import register_detection_module
from super_gradients.modules import ConvBNReLU
from super_gradients.modules.detection_modules import BaseDetectionModule
from super_gradients.training.models.detection_models.csp_darknet53 import width_multiplier
from super_gradients.training.models.detection_models.pp_yolo_e.pp_yolo_head import generate_anchors_for_grid_cell, PPYOLOEHead
from super_gradients.training.utils import HpmStruct, torch_version_is_greater_or_equal
from super_gradients.training.utils.bbox_utils import batch_distance2bbox


@register_detection_module("DeciYOLODFLHead")
class DeciYOLODFLHead(BaseDetectionModule):
    def __init__(self, in_channels: int, inter_channels: int, width_mult: float, first_conv_group_size: int, num_classes: int, stride: int, reg_max: int):
        super().__init__(in_channels)

        inter_channels = width_multiplier(inter_channels, width_mult, 8)
        if first_conv_group_size == 0:
            groups = 0
        elif first_conv_group_size == -1:
            groups = 1
        else:
            groups = inter_channels // first_conv_group_size

        self.num_classes = num_classes
        self.stem = ConvBNReLU(in_channels, inter_channels, kernel_size=1, stride=1, padding=0, bias=False)

        first_cls_conv = [ConvBNReLU(inter_channels, inter_channels, kernel_size=3, stride=1, padding=1, groups=groups, bias=False)] if groups else []
        self.cls_convs = nn.Sequential(*first_cls_conv, ConvBNReLU(inter_channels, inter_channels, kernel_size=3, stride=1, padding=1, bias=False))

        first_reg_conv = [ConvBNReLU(inter_channels, inter_channels, kernel_size=3, stride=1, padding=1, groups=groups, bias=False)] if groups else []
        self.reg_convs = nn.Sequential(*first_reg_conv, ConvBNReLU(inter_channels, inter_channels, kernel_size=3, stride=1, padding=1, bias=False))

        self.cls_pred = nn.Conv2d(inter_channels, self.num_classes, 1, 1, 0)
        self.reg_pred = nn.Conv2d(inter_channels, 4 * (reg_max + 1), 1, 1, 0)

        self.grid = torch.zeros(1)
        self.stride = stride

        self.prior_prob = 1e-2
        self._initialize_biases()

    @property
    def out_channels(self):
        return None

    def forward(self, x):
        x = self.stem(x)

        cls_feat = self.cls_convs(x)
        cls_output = self.cls_pred(cls_feat)

        reg_feat = self.reg_convs(x)
        reg_output = self.reg_pred(reg_feat)

        return reg_output, cls_output

    def _initialize_biases(self):
        prior_bias = -math.log((1 - self.prior_prob) / self.prior_prob)
        torch.nn.init.constant_(self.cls_pred.bias, prior_bias)

    @staticmethod
    def _make_grid(nx=20, ny=20):
        if torch_version_is_greater_or_equal(1, 10):
            # https://github.com/pytorch/pytorch/issues/50276
            yv, xv = torch.meshgrid([torch.arange(ny), torch.arange(nx)], indexing="ij")
        else:
            yv, xv = torch.meshgrid([torch.arange(ny), torch.arange(nx)])
        return torch.stack((xv, yv), 2).view((1, 1, ny, nx, 2)).float()


@register_detection_module("NDFLHeads")
class NDFLHeads(BaseDetectionModule, PPYOLOEHead):
    def __init__(
        self,
        num_classes: int,
        in_channels: Tuple[int, int, int],
        heads_list: Union[str, HpmStruct, DictConfig],
        fpn_strides: Tuple[int, int, int] = (8, 16, 32),
        grid_cell_scale=5.0,
        grid_cell_offset=0.5,
        reg_max=16,
        eval_size: Tuple[int, int] = None,
        width_mult: float = 1.0,
    ):
        """

        :param num_classes:
        :param in_channels: Number of channels for each feature map (See width_mult)
        :param activation: Type of the activation used in module
        :param fpn_strides: Output strides of the feature maps from the neck
        :param grid_cell_scale:
        :param grid_cell_offset:
        :param reg_max:
        :param eval_size: (rows, cols) Size of the image for evaluation. Setting this value can be beneficial for inference speed,
               since anchors will not be regenerated for each forward call.
        :param exclude_nms:
        :param exclude_post_process:
        :param width_mult: A scaling factor applied to in_channels in order.
        """
        super(NDFLHeads, self).__init__(in_channels)
        in_channels = [max(round(c * width_mult), 1) for c in in_channels]

        self.in_channels = tuple(in_channels)
        self.num_classes = num_classes
        self.fpn_strides = fpn_strides
        self.grid_cell_scale = grid_cell_scale
        self.grid_cell_offset = grid_cell_offset
        self.reg_max = reg_max
        self.eval_size = eval_size

        # Do not apply quantization to this tensor
        proj = torch.linspace(0, self.reg_max, self.reg_max + 1).reshape([1, self.reg_max + 1, 1, 1])
        self.register_buffer("proj_conv", proj, persistent=False)

        self._init_weights()

        factory = det_factory.DetectionModulesFactory()
        heads_list = self._pass_args(heads_list, factory, num_classes, reg_max)

        self.num_heads = len(heads_list)
        for i in range(self.num_heads):
            new_head = factory.get(factory.insert_module_param(heads_list[i], "in_channels", in_channels[i]))
            setattr(self, f"head{i + 1}", new_head)

    @staticmethod
    def _pass_args(heads_list, factory, num_classes, reg_max):
        for i in range(len(heads_list)):
            heads_list[i] = factory.insert_module_param(heads_list[i], "num_classes", num_classes)
            heads_list[i] = factory.insert_module_param(heads_list[i], "reg_max", reg_max)
        return heads_list

    @torch.jit.ignore
    def cache_anchors(self, input_size: Tuple[int, int]):
        self.eval_size = input_size
        anchor_points, stride_tensor = self._generate_anchors()
        self.anchor_points = anchor_points
        self.stride_tensor = stride_tensor

    @torch.jit.ignore
    def _init_weights(self):
        if self.eval_size:
            anchor_points, stride_tensor = self._generate_anchors()
            self.anchor_points = anchor_points
            self.stride_tensor = stride_tensor

    @torch.jit.ignore
    def forward_train(self, feats: Tuple[Tensor, ...]):
        anchors, anchor_points, num_anchors_list, stride_tensor = generate_anchors_for_grid_cell(
            feats, self.fpn_strides, self.grid_cell_scale, self.grid_cell_offset
        )

        cls_score_list, reg_distri_list = [], []
        for i, feat in enumerate(feats):
            reg_distri, cls_logit = getattr(self, f"head{i + 1}")(feat)
            # cls and reg
            # Note we don't apply sigmoid on class predictions to ensure good numerical stability at loss computation
            cls_score_list.append(torch.permute(cls_logit.flatten(2), [0, 2, 1]))
            reg_distri_list.append(torch.permute(reg_distri.flatten(2), [0, 2, 1]))
        cls_score_list = torch.cat(cls_score_list, dim=1)
        reg_distri_list = torch.cat(reg_distri_list, dim=1)

        return cls_score_list, reg_distri_list, anchors, anchor_points, num_anchors_list, stride_tensor

    def forward_eval(self, feats: Tuple[Tensor, ...]) -> Tuple[Tuple[Tensor, Tensor], Tuple[Tensor, ...]]:

        cls_score_list, reg_distri_list, reg_dist_reduced_list = [], [], []

        for i, feat in enumerate(feats):
            b, _, h, w = feat.shape
            height_mul_width = h * w
            reg_distri, cls_logit = getattr(self, f"head{i + 1}")(feat)
            reg_distri_list.append(torch.permute(reg_distri.flatten(2), [0, 2, 1]))

            reg_dist_reduced = torch.permute(reg_distri.reshape([-1, 4, self.reg_max + 1, height_mul_width]), [0, 2, 3, 1])
            reg_dist_reduced = torch.nn.functional.conv2d(torch.nn.functional.softmax(reg_dist_reduced, dim=1), weight=self.proj_conv).squeeze(1)

            # cls and reg
            cls_score_list.append(cls_logit.reshape([b, self.num_classes, height_mul_width]))
            reg_dist_reduced_list.append(reg_dist_reduced)

        cls_score_list = torch.cat(cls_score_list, dim=-1)  # [B, C, Anchors]
        cls_score_list = torch.permute(cls_score_list, [0, 2, 1])  # # [B, Anchors, C]

        reg_distri_list = torch.cat(reg_distri_list, dim=1)  # [B, Anchors, 4 * (self.reg_max + 1)]
        reg_dist_reduced_list = torch.cat(reg_dist_reduced_list, dim=1)  # [B, Anchors, 4]

        # Decode bboxes
        # Note in eval mode, anchor_points_inference is different from anchor_points computed on train
        if self.eval_size:
            anchor_points_inference, stride_tensor = self.anchor_points, self.stride_tensor
        else:
            anchor_points_inference, stride_tensor = self._generate_anchors(feats)

        pred_scores = cls_score_list.sigmoid()
        pred_bboxes = batch_distance2bbox(anchor_points_inference, reg_dist_reduced_list) * stride_tensor  # [B, Anchors, 4]

        decoded_predictions = pred_bboxes, pred_scores

        if torch.jit.is_tracing():
            return decoded_predictions

        anchors, anchor_points, num_anchors_list, _ = generate_anchors_for_grid_cell(feats, self.fpn_strides, self.grid_cell_scale, self.grid_cell_offset)

        raw_predictions = cls_score_list, reg_distri_list, anchors, anchor_points, num_anchors_list, stride_tensor
        return decoded_predictions, raw_predictions

    @property
    def out_channels(self):
        return None

    def forward(self, feats: Tuple[Tensor]):
        if self.training:
            return self.forward_train(feats)
        else:
            return self.forward_eval(feats)
