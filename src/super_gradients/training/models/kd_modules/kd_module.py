from super_gradients.training.models.sg_module import SgModule
from super_gradients.training.utils.utils import HpmStruct
from super_gradients.training.utils import get_param
from collections import namedtuple


KDOutput = namedtuple('KDOutput', 'student_output teacher_output')


class KDModule(SgModule):
    """
    KDModule

    class implementing Knowledge Distillation logic as an SgModule

    attributes:
        student: SgModule - the student model
        teacher: torch.nn.Module- the teacher model
        run_teacher_on_eval: bool- whether to run self.teacher at eval mode regardless of self.train(mode)
    """
    def __init__(self, arch_params: HpmStruct):
        super(KDModule, self).__init__()
        self.student = arch_params.student
        self.teacher = arch_params.teacher
        self.run_teacher_on_eval = get_param(arch_params, "run_teacher_on_eval", False)
        self._freeze_teacher()

        # WHEN CREATING A MODULE SELF.TRAIN() ISN'T CALLED AND SO THE TEACHER MUST BE MOVED TO EVAL MODE EXPLICITLY
        if self.run_teacher_on_eval:
            self.teacher.eval()

    def _freeze_teacher(self):
        for p in self.teacher.parameters():
            p.requires_grad = False

    def train(self, mode=True):
        self.student.train(mode)
        if not self.run_teacher_on_eval:
            self.teacher.train(mode)

    def eval(self):
        self.student.eval()
        self.teacher.eval()

    def forward(self, x):
        return KDOutput(student_output=self.student(x),
                        teacher_output=self.teacher(x))

    def initialize_param_groups(self, lr: float, training_params: HpmStruct) -> list:
        return self.student.initialize_param_groups(lr, training_params)

    def update_param_groups(self, param_groups: list, lr: float, epoch: int, iter: int, training_params: HpmStruct,
                            total_batch: int) -> list:
        return self.student.update_param_groups(param_groups, lr, epoch, iter, training_params, total_batch)

    def replace_head(self, **kwargs):
        self.student.replace_head(**kwargs)
