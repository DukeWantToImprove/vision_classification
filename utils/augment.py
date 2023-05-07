from PIL import Image
from typing import Optional, List, Tuple, Callable
import numpy as np
import random
import torchvision.transforms as T
from functools import wraps

# all methods based on PIL
__all__ = ['color_jitter', 'random_horizonflip', 'random_verticalflip', 'to_tensor', 'normalize', 'random_augment',
           'center_crop', 'resize', 'random_cutout','random_affine', 'create_AugTransforms',]

class _RandomApply: # decorator
    def __init__(self, prob):
        self.prob = prob

    def __call__(self, func):
        def wrapper(*args, **kwargs):
            if random.random() < self.prob:
                return func(*args,**kwargs)
            return lambda x: x
        return wrapper

AUG_METHODS = {}
def register_method(fn: Callable):
    key = fn.__name__
    if key in AUG_METHODS:
        raise ValueError(f"An entry is already registered under the name '{key}'.")
    AUG_METHODS[key] = fn
    @wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapper

class Cutout:
    """Randomly mask out one or more patches from an image.
    Args:
        n_holes (int): Number of patches to cut out of each image.
        length (int): The length (in pixels) of each square patch.
    """
    def __init__(self, n_holes: int, length: int, ratio: float,
                 h_range: Optional[List[int]] = None, w_range: Optional[List[int]] = None,
                 prob: float = 0.5):
        self.n_holes = n_holes
        self.length = length
        self.ratio = ratio
        self.h_range = h_range
        self.w_range = w_range
        self.prob = prob

    def __call__(self, img):
        """
        Args:
            img (Tensor): Tensor image of size (C, H, W) from PIL
        Returns:
            PIL: Image with n_holes of dimension length x length cut out of it.
        """
        if random.random() > self.prob:
            return img
        img_h = img.size[1]
        img_w = img.size[0]
        h = self.h_range if self.h_range is not None else [0, img_h] # PIL Image size->(w,h)
        w = self.w_range if self.w_range is not None else [0, img_w]

        mask_w = int(random.uniform(1-self.ratio, 1+self.ratio) * self.length)
        mask_h = self.length
        mask = Image.new('RGB', size=(mask_w, mask_h), color=0)

        for n in range(self.n_holes):
            # center
            y = np.random.randint(*h)
            x = np.random.randint(*w)

            # left-up
            x1 = max(0, x - self.length // 2)
            y1 = max(0, y - self.length // 2)

            img.paste(mask, (x1, y1))

        return  img

@register_method
def random_cutout(n_holes:int = 1, length: int = 200, ratio: float = 0.2,
                  h_range: Optional[List[int]] = None, w_range: Optional[List[int]] = None, prob: float = 0.5):
    return Cutout(n_holes, length, ratio, h_range, w_range, prob)

@register_method
def color_jitter(brightness: float = 0.,
                 contrast: float = 0.,
                 saturation: float = 0.,
                 hue: float = 0.):
    return T.ColorJitter(brightness=brightness, contrast=contrast, saturation=saturation, hue=hue)

@register_method
def random_horizonflip(p: float = 0.5):
    return T.RandomHorizontalFlip(p=p)

@register_method
def random_verticalflip(p: float = 0.5):
    return T.RandomVerticalFlip(p=p)

@register_method
def to_tensor():
    return T.ToTensor()

@register_method
def normalize(mean: Tuple = (0.485, 0.456, 0.406), std: Tuple = (0.229, 0.224, 0.225)):
    return T.Normalize(mean=mean, std=std)

@register_method
def random_augment(num_ops: int = 2, magnitude: int = 9, num_magnitude_bins: int = 31,):
    return T.RandAugment(num_ops=num_ops, magnitude=magnitude, num_magnitude_bins=num_magnitude_bins)

@register_method
def center_crop(size):# size (sequence or int) -> square or rectangle
    return T.CenterCrop(size=size)

@register_method
def resize(size = 224):
    # size (sequence or int) -> square or rectangle: Desired output size. If size is a sequence like
    # (h, w), output size will be matched to this. If size is an int,smaller
    # edge of the image will be matched to this number. i.e,
    # if height > width, then image will be rescaled to (size * height / width, size).
    return T.Resize(size = size)

@register_method
def random_affine(degrees = 0., translate = 0., scale = 0., shear = 0., fill=0, center=None):
    return T.RandomAffine(degrees=degrees, translate=translate, scale=scale, shear=shear, fill=fill, center=center)

def create_AugTransforms(augments: str):
    augments = augments.strip().split()
    return T.Compose(tuple(map(lambda x: AUG_METHODS[x](), augments)))

def list_augments():
    augments = [k for k, v in AUG_METHODS.items()]
    return sorted(augments)