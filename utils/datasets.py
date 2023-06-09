import os
import torch
from torch.utils.data import Dataset
import json
from PIL import Image
from pathlib import Path


class Datasets(Dataset):
    def __init__(self, root, mode, transforms = None, label_transforms = None, project = None, rank = None):
        assert os.path.isdir(root), "dataset root: {} does not exist.".format(root)
        src_path = os.path.join(root, mode)
        data_class = [cla for cla in os.listdir(src_path) if os.path.isdir(os.path.join(src_path, cla))]
        # sort
        data_class.sort()

        class_indices = dict((k, v) for v, k in enumerate(data_class))
        if rank in {-1, 0}:
            json_str = json.dumps(dict((val, key) for key, val in class_indices.items()), indent=4)
            os.makedirs('./run', exist_ok=True)
            if project is not None:
                with open(Path(project) / 'class_indices.json', 'w') as json_file:
                    json_file.write(json_str)

        support = [".jpg", ".png"]

        images_path = []  # image path
        images_label = []  # label idx

        for cla in data_class:
            cla_path = os.path.join(src_path, cla)
            images = [os.path.join(src_path, cla, i) for i in os.listdir(cla_path)
                      if os.path.splitext(i)[-1] in support]
            image_class = class_indices[cla]
            for img_path in images:
                images_path.append(img_path)
                images_label.append(image_class)

        self.images = images_path
        self.labels = images_label
        self.transforms = transforms
        self.label_transforms = label_transforms
        self.class_indices = data_class


    def __getitem__(self, idx):
        img = Image.open(self.images[idx]).convert('RGB')
        label = self.labels[idx]
        if self.transforms is not None:
            img = self.transforms(img)
        if self.label_transforms is not None:
            label = self.label_transforms(label)

        return img, label


    def __len__(self):
        return len(self.images)

    @staticmethod
    def collate_fn(batch):
        imgs, labels = tuple(zip(*batch))

        imgs = torch.stack(imgs, dim=0)
        labels = torch.as_tensor(labels) if isinstance(labels[0], int) else torch.stack(labels, dim=0)
        return imgs, labels
