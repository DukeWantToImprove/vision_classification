import torch
import torchvision
import torch.nn as nn
from typing import Callable
from torch.cuda.amp import GradScaler
from torch.nn.init import normal_, constant_
from .datasets import Datasets
from .augment import create_AugTransforms
from torch.utils.data import DataLoader
from .logger import SmartLogger
from .optimizer import create_Optimizer
from .scheduler import create_Scheduler
from .loss import create_Lossfn
from torch.utils.data import DistributedSampler
from .train import train_one_epoch
from functools import reduce, partial
from pathlib import Path
from torch.nn.parallel import DistributedDataParallel as DDP
from .plots import colorstr
import time
import os
import datetime
import yaml
from copy import deepcopy

__all__ = ['yaml_load', 'SmartModel', 'SmartDataProcessor', 'CenterProcessor','increment_path', 'check_cfgs']


def yaml_load(file='data.yaml'):
    # Single-line safe yaml loading
    with open(file, errors='ignore') as f:
        return yaml.safe_load(f)


def increment_path(path, exist_ok=False, sep='', mkdir=False):
    # Increment file or directory path, i.e. runs/exp --> runs/exp{sep}2, runs/exp{sep}3, ... etc.
    path = Path(path)  # os-agnostic
    if path.exists() and not exist_ok:
        path, suffix = (path.with_suffix(''), path.suffix) if path.is_file() else (path, '')

        # Method 1
        for n in range(2, 9999):
            p = f'{path}{sep}{n}{suffix}'  # increment path
            if not os.path.exists(p):  #
                break
        path = Path(p)

        # Method 2 (deprecated)
        # dirs = glob.glob(f"{path}{sep}*")  # similar paths
        # matches = [re.search(rf"{path.stem}{sep}(\d+)", d) for d in dirs]
        # i = [int(m.groups()[0]) for m in matches if m]  # indices
        # n = max(i) + 1 if i else 2  # increment number
        # path = Path(f"{path}{sep}{n}{suffix}")  # increment path

    if mkdir:
        path.mkdir(parents=True, exist_ok=True)  # make directory

    return path

def check_cfgs(cfgs):
    model_cfg = cfgs['model']
    data_cfg = cfgs['data']
    hyp_cfg = cfgs['hyp']

    assert model_cfg['choice'].split('-')[0] in {'torchvision', 'custom'}, 'if from torchvision, torchvision-ModelName; if from your own, custom-ModelName'
    assert (model_cfg['pretrained'] and ('normalize' in data_cfg['train']['augment'].split())) or \
           (not model_cfg['pretrained']) and ('normalize' not in data_cfg['train']['augment'].split()),\
           'if not pretrained, normalize is not necessary'

    assert reduce(lambda x, y: int(x) + int(y), list(hyp_cfg['loss'].values())) == 1, 'ce or bce'
    if hyp_cfg['strategy']['focal'].split()[0] == '1': assert hyp_cfg['loss']['bce'], 'focalloss only support bceloss'
    assert hyp_cfg['optimizer'] in {'sgd', 'adam'}, 'optimizer choose sgd or adam'
    assert hyp_cfg['scheduler'] in {'linear', 'cosine'}, 'scheduler support linear or cosine'
    mixup, mixup_milestone = map(eval, hyp_cfg['strategy']['mixup'].split())
    assert mixup >= 0 and mixup <= 1 and isinstance(mixup_milestone, int), 'mixup_ratio[0,1], mixup_milestone be int'

class SmartModel:
    def __init__(self, model_cfgs: dict):
        self.model_cfgs = model_cfgs

        self.num_classes = model_cfgs['num_classes']
        self.pretrained = model_cfgs['pretrained']
        self.backbone_freeze = model_cfgs['backbone_freeze']
        self.bn_freeze = model_cfgs['bn_freeze']
        self.bn_freeze_affine = model_cfgs['bn_freeze_affine']

        model_cfgs_copy = deepcopy(model_cfgs)
        model_cfgs_copy['kind'], model_cfgs_copy['choice'] = model_cfgs['choice'].split('-')

        # init model
        self.model = self.create_model(**model_cfgs_copy)
        del model_cfgs_copy

        if not self.pretrained: self.reset_parameters()

    def create_model(self, choice: str, num_classes: int = 1000, pretrained: bool = False, kind: str = 'torchvision',
                     backbone_freeze: bool = False, bn_freeze: bool = False, bn_freeze_affine: bool = False):
        assert kind in {'torchvision', 'custom'}, 'kind must be torchvision or custom'
        if kind == 'torchvision':
            model = torchvision.models.get_model(choice, weights = torchvision.models.get_model_weights(choice) if pretrained else None)
            model.fc = nn.Linear(model.fc.in_features, num_classes)

        else:
            pass
        if backbone_freeze: self.freeze_backbone()
        if bn_freeze: self.freeze_bn(bn_freeze_affine)
        return model

    def init_parameters(self, m: nn.Module):
        if isinstance(m, nn.Conv2d):
            normal_(m.weight, mean=0, std=0.02)
        elif isinstance(m, nn.BatchNorm2d):
            constant_(m.weight, 1)
            constant_(m.bias, 0)
        elif isinstance(m, nn.Linear):
            normal_(m.weight, mean=0, std=0.02)
            constant_(m.bias, 0)

    def reset_parameters(self):
        self.model.apply(self.init_parameters)

    def freeze_backbone(self):
        kind, _ = self.model_cfgs['choice'].split('-')
        if kind == 'torchvision':
            for name, m in self.model.named_children():
                if name != 'fc':
                    for p in m.parameters():
                        p.requires_grad_(False)
            print('backbone freeze')
        else: pass

    def freeze_bn(self, bn_freeze_affine: bool = False):
        for m in self.model.modules():
            if isinstance(m, nn.BatchNorm2d):
                m.eval()
                if bn_freeze_affine:
                    m.weight.requires_grad_(False)
                    m.bias.requires_grad_(False)

class SmartDataProcessor:
    def __init__(self, data_cfgs: dict, rank, project):
        self.data_cfgs = data_cfgs # root, train, val
        self.rank = rank
        self.project = project
        self.label_transforms = None # used in CenterProcessor.__init__

        self.train_dataset = self.create_dataset('train')
        self.val_dataset = self.create_dataset('val')

    def create_dataset(self, mode: str):
        assert mode in {'train', 'val'}

        cfg = self.data_cfgs.get(mode, -1)
        if isinstance(cfg, dict):
            dataset = Datasets(root=self.data_cfgs['root'], mode=mode,
                               transforms=create_AugTransforms(augments=cfg['augment']),
                               project=self.project, rank=self.rank)
        else: dataset = None
        return dataset

    def set_augment(self, mode: str, sequence = None): # sequence -> T.Compose([...])
        if sequence is None:
            sequence = self.val_dataset.transforms
        dataset = getattr(self, f'{mode}_dataset')
        dataset.transforms = sequence

    def auto_aug_weaken(self, epoch: int, milestone: int): # only flip
        if epoch == milestone:
            sequence = create_AugTransforms('random_horizonflip to_tensor normalize')
            self.set_augment('train', sequence)

    @staticmethod
    def set_label_transforms(label, num_classes, label_smooth): # idx -> vector
        vector = torch.zeros(num_classes).fill_(0.5 * label_smooth)
        vector[label] = 1 - 0.5 * label_smooth

        return vector

    @staticmethod
    def set_dataloader(dataset, bs: int = 256, nw: int = 0, pin_memory: bool = True, shuffle: bool = True, sampler = None):
        assert not (shuffle and sampler is not None)
        nd = torch.cuda.device_count()
        nw = min([os.cpu_count() // max(nd, 1), nw])
        return DataLoader(dataset=dataset, batch_size=bs, num_workers=nw, pin_memory=pin_memory, sampler=sampler, shuffle=shuffle)

class CenterProcessor:
    def __init__(self, cfgs, rank, project):
        filename = Path(project) / "log{}.log".format(datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        self.project = project
        if rank in {-1, 0}: project.mkdir(parents=True, exist_ok=True)

        self.model_cfg = cfgs['model']
        self.data_cfg = cfgs['data']
        self.hyp_cfg = cfgs['hyp']

        # rank
        self.rank: int = rank
        # device
        if rank != -1:
            device = torch.device('cuda', rank)
        else:
            device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.device: torch.device = device

        # model processor
        self.model_processor = SmartModel(self.model_cfg)
        self.model_processor.model.to(device)
        # data processor
        self.data_processor = SmartDataProcessor(self.data_cfg, rank=rank, project=project)
        # logger
        self.logger = SmartLogger(filename=filename, level=1) if rank in {-1,0} else None
        if self.logger is not None and rank in {-1, 0}:
            self.logger.both(cfgs) # output configs
        # optimizer
        self.optimizer = create_Optimizer(optimizer=self.hyp_cfg['optimizer'], lr=self.hyp_cfg['lr0'],
                                          weight_decay=self.hyp_cfg['weight_decay'], momentum=self.hyp_cfg['warmup_momentum'],
                                          params=[p for p in self.model_processor.model.parameters() if p.requires_grad])
        # scheduler
        self.scheduler = create_Scheduler(scheduler=self.hyp_cfg['scheduler'], optimizer=self.optimizer,
                                          warm_ep=self.hyp_cfg['warm_ep'], epochs=self.hyp_cfg['epochs'], lr0=self.hyp_cfg['lr0'],lrf_ratio=self.hyp_cfg['lrf_ratio'])
        # loss
        loss_choice: str = [k for k, v in self.hyp_cfg['loss'].items() if v][0]
        self.lossfn = create_Lossfn(loss_choice)() \
            if loss_choice == 'bce' \
            else create_Lossfn(loss_choice)(label_smooth = self.hyp_cfg['label_smooth'])

        # train_one_epoch
        self.train_one_epoch: Callable = train_one_epoch # include val

        # add label_transforms
        if loss_choice == 'bce':
            self.data_processor.train_dataset.label_transforms = \
                partial(SmartDataProcessor.set_label_transforms,
                        num_classes = self.model_cfg['num_classes'],
                        label_smooth = self.hyp_cfg['label_smooth'])
            self.data_processor.val_dataset.label_transforms = \
                partial(SmartDataProcessor.set_label_transforms,
                        num_classes=self.model_cfg['num_classes'],
                        label_smooth=self.hyp_cfg['label_smooth'])

        self.loss_choice = loss_choice
        # distributions sampler
        self.dist_sampler = self._distributions_sampler()

        # focalloss hard
        if loss_choice == 'bce' and self.hyp_cfg['strategy']['focal'].split()[0] == '1':
            self.focal = create_Lossfn('focal')()
        else:
            self.focal = None
        self.focal_eff_epo = eval(self.hyp_cfg['strategy']['focal'].split()[1])
        # on or off
        self.focal_on = eval(self.hyp_cfg['strategy']['focal'].split()[0])

    def set_optimizer_momentum(self, momentum) -> None:
        self.optimizer.param_groups[0]['momentum'] = momentum

    def _distributions_sampler(self):
        d = {}

        d['uniform'] = torch.distributions.uniform.Uniform(low=0, high=1)
        d['beta'] = torch.distributions.beta.Beta(32, 32)

        return d

    def auto_replace_lossfn(self, lossfn = None, cur_epo = None, effect_epo = None, on = None):
        if not on or lossfn: return
        if cur_epo == effect_epo:
            self.lossfn = lossfn
            # set 0
            self.focal_on = 0

    def auto_mixup(self, mixup: float, epoch:int, milestone: int):
        if mixup == 0 or epoch >= milestone: return (False, None) # is_mixup, lam
        else:
            mix_prob = self.dist_sampler['uniform'].sample()
            is_mixup: bool = mix_prob < mixup
            lam = self.dist_sampler['beta'].sample().to(self.device)
            return is_mixup, lam

    def set_sync_bn(self):
        self.model_processor.model = nn.SyncBatchNorm.convert_sync_batchnorm(module=self.model_processor.model)

    def run(self, resume = None): # train+val per epoch
        last, best = self.project / 'last.pt', self.project / 'best.pt'
        model, data_processor, lossfn, optimizer, scaler, device, epochs, logger, (mixup, mixup_milestone), rank, distributions_sampler, warm_ep, aug_epoch, scheduler, focal = \
            self.model_processor.model, self.data_processor, self.lossfn, self.optimizer, \
            GradScaler(enabled = (self.device != torch.device('cpu'))), self.device, self.hyp_cfg['epochs'], \
            self.logger, self.hyp_cfg['strategy']['mixup'].split(), self.rank, self.dist_sampler, self.hyp_cfg['warm_ep'], \
            self.data_cfg['train']['aug_epoch'], self.scheduler, self.focal

        # data
        train_dataset, val_dataset = data_processor.train_dataset, data_processor.val_dataset
        data_sampler = None if self.rank == -1 else DistributedSampler(dataset=train_dataset)
        train_dataloader = data_processor.set_dataloader(dataset=train_dataset,
                                                         bs=self.data_cfg['train']['bs'],
                                                         nw=self.data_cfg['nw'],
                                                         pin_memory=True,
                                                         sampler=data_sampler,
                                                         shuffle=data_sampler is None)
        if self.rank in {-1, 0}:
            val_dataloader = data_processor.set_dataloader(dataset=val_dataset,
                                                           bs=self.data_cfg['val']['bs'],
                                                           nw=self.data_cfg['nw'],
                                                           pin_memory=False,
                                                           shuffle=False)
        else: val_dataloader = None
        best_fitness = 0.
        start_epoch = 0

        # resume
        if resume is not None:
            ckp = torch.load(resume, map_location=device)
            start_epoch = ckp['epoch'] + 1
            best_fitness = ckp['best_fitness']
            model.load_state_dict(ckp['model'])
            optimizer.load_state_dict(ckp['optimizer'])
            scheduler.load_state_dict(ckp['scheduler'])
            if device != torch.device('cpu'):
                scaler.load_state_dict(ckp['scaler'])

        if rank != -1:
            model = DDP(model, device_ids=[self.rank])

        if self.rank in {-1, 0}:
            time.sleep(0.1)
            print(f"{'Epoch':>10}{'GPU_mem':>10}{'train_loss':>12}{f'val_loss':>12}{'top1_acc':>12}{'top5_acc':>12}")

        t0 = time.time()
        total_epoch = epochs+warm_ep
        for epoch in range(start_epoch, total_epoch):
            # change optimizer momentum from warm_moment0.8 -> momentum0.937
            if epoch == warm_ep:
                self.set_optimizer_momentum(self.hyp_cfg['momentum'])
            # change lossfn bce -> focal
            self.auto_replace_lossfn(self.focal, int(epoch-warm_ep), self.focal_eff_epo, self.focal_on)

            # weaken data augment at milestone
            self.data_processor.auto_aug_weaken(int(epoch-warm_ep), milestone=aug_epoch)

            # mixup epoch-wise
            is_mixup, lam = self.auto_mixup(mixup=eval(mixup), epoch=int(epoch-warm_ep), milestone=eval(mixup_milestone))

            # train for one epoch
            fitness = self.train_one_epoch(model, train_dataloader, val_dataloader, lossfn, optimizer,scaler, device, epoch, epochs, logger, is_mixup, rank, lam, scheduler)

            if rank in {-1, 0}:
                # Best fitness
                if fitness > best_fitness:
                    best_fitness = fitness

                # Save model
                final_epoch: bool = epoch + 1 == total_epoch
                ckpt = {
                    'epoch': epoch,
                    'best_fitness': best_fitness,
                    'model': model.state_dict() if rank == -1 else model.module.state_dict(),  # deepcopy(de_parallel(model)).half(),
                    'optimizer': optimizer.state_dict(),  # optimizer.state_dict(),
                    'scheduler': scheduler.state_dict(),
                }
                if device != torch.device('cpu'):
                    ckpt['scaler'] = scaler.state_dict()

                # Save last, best and delete
                torch.save(ckpt, last)
                if best_fitness == fitness:
                    torch.save(ckpt, best)
                del ckpt

                # complete
                if final_epoch:
                    logger.console(f'\nTraining complete ({(time.time() - t0) / 3600:.3f} hours)'
                                   f"\nResults saved to {colorstr('bold', self.project)}"
                                   f'\nPredict:         python predict.py --weight {best} --root data/val/{colorstr("blue", "XXX_cls")} --txt_path --choice {self.model_cfg["choice"]} --class_head {self.loss_choice} --class_json {self.project}/class_indices.json --num_classes {self.model_cfg["num_classes"]} --transforms "{self.data_cfg["val"]["augment"]}"'
                                   f'\nValidate:        python val.py --weight {best} --choice {self.model_cfg["choice"]} --root {colorstr("blue", "data")} --num_classes {self.model_cfg["num_classes"]} --transforms "{self.data_cfg["val"]["augment"]}"')