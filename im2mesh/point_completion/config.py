import torch
from torchvision import transforms
from torch import nn
import os
from im2mesh import data
from im2mesh import config
from im2mesh.encoder import encoder_dict
from im2mesh.point_completion import model as pc_model
from im2mesh.point_completion import training


def get_model(cfg, device=None, dataset=None, **kwargs):
    ''' Return the OccupancyWithDepth Network model.

    Args:
        cfg (dict): imported yaml config 
        device (device): pytorch device
        dataset (dataset): dataset
    '''
    encoder = cfg['model']['encoder']
    assert encoder.startswith('point')

    c_dim = cfg['model']['c_dim']
    encoder_kwargs = cfg['model']['encoder_kwargs']

    if 'input_points_count' in cfg['model']:
        input_points_count = cfg['model']['input_points_count']
    else:
        input_points_count = 2048

    if 'output_points_count' in cfg['model']:
        output_points_count = cfg['model']['output_points_count']
    else:
        output_points_count = 2048

    encoder = encoder_dict[encoder](
        c_dim=c_dim,
        **encoder_kwargs
    )


    model = pc_model.PointCompletionNetwork(encoder, device=device, c_dim=c_dim,
        input_points_count=input_points_count, 
        output_points_count=output_points_count, 
        preserve_input=False
    )

    return model


def get_trainer(model, optimizer, cfg, device, **kwargs):
    ''' Returns the trainer object.

    Args:
        model (nn.Module): the Occupancy Network model
        optimizer (optimizer): pytorch optimizer object
        cfg (dict): imported yaml config
        device (device): pytorch device
    '''
    out_dir = cfg['training']['out_dir']
    vis_dir = os.path.join(out_dir, 'vis')
    input_type = cfg['data']['input_type']
    
    trainer_params = {}

    if 'depth_pointcloud_transfer' in cfg['model']:
        trainer_params['depth_pointcloud_transfer'] = cfg['model']['depth_pointcloud_transfer']

    if 'gt_pointcloud_transfer' in cfg['model']:
        trainer_params['gt_pointcloud_transfer'] = cfg['model']['gt_pointcloud_transfer']

    trainer = training.PointCompletionTrainer(model, optimizer,
        device=device, input_type=input_type,
        vis_dir=vis_dir, **trainer_params
    )
    return trainer


def get_data_fields(mode, cfg):
    ''' Returns the data fields.

    Args:
        mode (str): the mode which is used
        cfg (dict): imported yaml config
    '''
    fields = {}
    if 'output_points_count' in cfg['model']:
        output_points_count = cfg['model']['output_points_count']
    else:
        output_points_count = 2048

    transform = transforms.Compose([
        data.SubsamplePointcloud(output_points_count)
    ])

    fields['pointcloud'] = data.PointCloudField(
        cfg['data']['pointcloud_file'], transform,
        with_transforms=True
    )
    
    return fields
