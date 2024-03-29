import numpy as np
import argparse
import time
import random
import os
from os import path as osp
from termcolor import colored
import pickle
import wandb
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
import torchvision.transforms as transforms
import torch.optim.lr_scheduler as lr_scheduler
from utils_training.optimize_SimSacNet import train_epoch, validate_epoch, test_epoch
# from utils_training.optimize_GLUChangeSiamNet import train_epoch, validate_epoch, test_epoch
from models.our_models.SimSacNet import SimSacNet, SimSacDecoder
from utils_training.utils_CNN import load_checkpoint, save_checkpoint, boolean_string
from tensorboardX import SummaryWriter
from utils.image_transforms import ArrayToTensor
from datasets.prepare_dataloaders import prepare_trainval,prepare_test
from datasets.prepare_transforms import prepare_transforms
from utils_training.prepare_optimizer import prepare_optim


def train_simsac():
    # weight and bias
    if not gargs.test_only and not gargs.not_logging:
        project = 'SCD' if gargs.tr_type == 'sup' else 'SCD_usl'
        run_name = None if gargs.run_name == '' else gargs.run_name
        wandb.init(config=gargs, project=project, entity='homebody', name=run_name)
        args = wandb.config
    else:
        args = gargs
    
    # set seed
    random.seed(args.seed)
    np.random.seed(args.seed)
    os.environ['PYTHONHASHSEED'] = str(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    
    # torch.backends.cudnn.benchmark = False
    # torch.use_deterministic_algorithms(True)
        
    """
    Dataloaders
    """
    # train set
    train_dataloader = {}
    if args.tr_type == 'sup':
        sup_src_img_trf, sup_tgt_img_trf ,sup_co_trf, sup_flow_trf, sup_change_trf = prepare_transforms(tr_type='sup')
        usl_src_img_trf, usl_tgt_img_trf ,usl_co_trf, usl_flow_trf, usl_change_trf = prepare_transforms(tr_type='sup')
    elif args.tr_type == 'usl':
        sup_src_img_trf, sup_tgt_img_trf ,sup_co_trf, sup_flow_trf, sup_change_trf = prepare_transforms(tr_type='usl')
        usl_src_img_trf, usl_tgt_img_trf ,usl_co_trf, usl_flow_trf, usl_change_trf = prepare_transforms(tr_type='usl')
    elif args.tr_type == 'semi':
        # semi-supervised
        sup_src_img_trf, sup_tgt_img_trf ,sup_co_trf, sup_flow_trf, sup_change_trf = prepare_transforms(tr_type='usl')
        usl_src_img_trf, usl_tgt_img_trf ,usl_co_trf, usl_flow_trf, usl_change_trf = prepare_transforms(tr_type='semi')

    sup_train_loader, val_dataloader, ri = prepare_trainval(args, 
                                                    args.sup_trainset_list,
                                                    sup_src_img_trf, sup_tgt_img_trf,
                                                    sup_flow_trf, sup_co_trf, sup_change_trf,
                                                    msg='Labeled dataset for Supervised loss',
                                                    subset_frac=args.sup_frac)
    usl_train_loader, _, _ = prepare_trainval(args, 
                                            args.usl_trainset_list,
                                            usl_src_img_trf, usl_tgt_img_trf,
                                            usl_flow_trf, usl_co_trf, usl_change_trf,
                                            msg='Unlabeled dataset for Unsupervised loss',
                                            subset_frac=args.usl_frac,
                                            ri=ri)
    train_dataloader['sup'] = sup_train_loader
    train_dataloader['usl'] = usl_train_loader
    
    # test set
    if args.tr_type == 'test':
        test_src_img_trf, test_tgt_img_trf ,test_co_trf, test_flow_trf, test_change_trf = prepare_transforms(tr_type='test')
    else:
        test_src_img_trf, test_tgt_img_trf ,test_co_trf, test_flow_trf, test_change_trf = prepare_transforms(tr_type='')
        
    test_dataloaders = prepare_test(args, source_img_transforms=test_src_img_trf,
                                target_img_transforms=test_tgt_img_trf,
                                flow_transform=test_flow_trf, 
                                co_transform=test_co_trf, 
                                change_transform=test_change_trf)


    """
    Model
    """
    model = SimSacNet(batch_norm=True, pyramid_type=args.pyramid_type,
                    dense_cl=args.cl_ptr_w,
                    div=args.div_flow, evaluation=False,
                    consensus_network=False,
                    cyclic_consistency=True,
                    dense_connection=True,
                    decoder_inputs='corr_flow_feat',
                    refinement_at_all_levels=False,
                    refinement_at_adaptive_reso=True,
                    num_class=5 if args.multi_class else 1,
                    cl=args.cl,
                    tr_type=args.tr_type,
                    use_pac = args.use_pac,
                    vpr_candidates=args.vpr_candidates,
                    t_param=args.t_param)
    # else:
    # model = GLUChangeSiamNet_model(batch_norm=True, pyramid_type=args.pyramid_type,
    #                                 dense_cl=args.cl_ptr_w,
    #                                 div=args.div_flow, evaluation=False,
    #                                 consensus_network=False,
    #                                 cyclic_consistency=True,
    #                                 dense_connection=True,
    #                                 decoder_inputs='corr_flow_feat',
    #                                 refinement_at_all_levels=False,
    #                                 refinement_at_adaptive_reso=True,
    #                                 num_class=5 if args.multi_class else 1,
    #                                 cl=args.cl,
    #                                 sg_dec=False,
    #                                 use_pac = args.use_pac,
    #                                 vpr_candidates=args.vpr_candidates)
    print(colored('==> ', 'blue') + 'GLU-Change-Net created.')

    # Optimizer
    optimizer, scheduler = prepare_optim(args,model)
    weights_loss_coeffs = [0.32, 0.08, 0.02, 0.01]

    if args.pretrained:
        if args.test_only:
            model, _, _, _, _, = load_checkpoint(model, None, None, filename=args.pretrained)
            # model.teacher_dec, _, _, _, _ = load_checkpoint(model.teacher_dec, None, None, filename=args.pretrained)
            # model, _, _, _, _ = load_checkpoint(model, None, None, filename=args.pretrained)
        else:
            # reload from pre_trained_model
            model, _, _, _, _ = load_checkpoint(model, None, None, filename=args.pretrained)
            model.teacher_dec, _, _, _, _ = load_checkpoint(model.teacher_dec, None, None, filename=args.pretrained)
            if args.tr_type == 'semi':
                model.student_dec, _, _, _, _ = load_checkpoint(model.student_dec, None, None, filename=args.pretrained)
                
        if not os.path.isdir(args.snapshots):
            os.mkdir(args.snapshots)

        cur_snapshot = args.name_exp
        if not osp.isdir(osp.join(args.snapshots, cur_snapshot)):
            os.makedirs(osp.join(args.snapshots, cur_snapshot))

        # with open(osp.join(args.snapshots, cur_snapshot, 'args.pkl'), 'wb') as f:
        #     pickle.dump(args, f)

        best_val = float("inf")
        start_epoch = 0

    elif args.resume:
        # reload from pre_trained_model
        model, optimizer, scheduler, start_epoch, best_val = load_checkpoint(model, optimizer, scheduler,
                                                                 filename=args.resume)
        # now individually transfer the optimizer parts...
        for state in optimizer.state.values():
            for k, v in state.items():
                if isinstance(v, torch.Tensor):
                    state[k] = v.to(device)
        cur_snapshot = os.path.basename(os.path.dirname(args.resume))
    else:
        if not os.path.isdir(args.snapshots):
            os.mkdir(args.snapshots)

        cur_snapshot = args.name_exp
        if not osp.isdir(osp.join(args.snapshots, cur_snapshot)):
            os.makedirs(osp.join(args.snapshots, cur_snapshot))

        with open(osp.join(args.snapshots, cur_snapshot, 'args.pkl'), 'wb') as f:
            pickle.dump(args, f)

        best_val = float("inf")
        start_epoch = 0

    # create summary writer
    save_path = osp.join(args.snapshots, cur_snapshot)
    train_writer = SummaryWriter(os.path.join(save_path, 'train'))
    val_writer = SummaryWriter(os.path.join(save_path, 'test'))
    
    model = nn.DataParallel(model)
    model = model.to(device)
    
    if args.t_param == 'share':
        del model.module.teacher_dec
        model.module.teacher_dec = model.module.student_dec
    elif args.t_param == 'ema':
        for param in model.module.teacher_dec.parameters():
            param.detach_()
    else:
        pass

    train_started = time.time()
    if not args.test_only:
        
        for epoch in range(start_epoch, args.n_epoch):
            print('starting epoch {}:  learning rate is {}'.format(epoch, scheduler.get_last_lr()[0]))
            train_loss = train_epoch(args, model,
                                     optimizer,
                                     train_dataloader,
                                     device,
                                     epoch,
                                     train_writer,
                                     loss_grid_weights=weights_loss_coeffs)
            scheduler.step()
            train_writer.add_scalar('train loss: flow(EPE)', train_loss['flow'], epoch)
            train_writer.add_scalar('train loss: change(FE)', train_loss['change'], epoch)
            train_writer.add_scalar('learning_rate', scheduler.get_last_lr()[0], epoch)
            print(colored('==> ', 'green') + 'Train average loss:', train_loss['total'])

            save_checkpoint({'epoch': epoch + 1,
                             'state_dict': model.module.state_dict(),
                             'optimizer': optimizer.state_dict(),
                             'scheduler': scheduler.state_dict(),
                             'best_loss': 9999999},
                            False, save_path, 'epoch_{}.pth'.format(epoch + 1))

            for dataset_name,test_dataloader in test_dataloaders.items():
                result = test_epoch(args, model, test_dataloader, device, epoch=epoch,
                           save_path=os.path.join(save_path, dataset_name),
                           writer=val_writer,
                           div_flow=args.div_flow,
                           plot_interval=args.plot_interval)
                print('          F1: {:.2f}, Accuracy: {:.2f} '.format(result['f1'], result['accuracy']))
                print('          Static  |   Change   |   mIoU ')
                print('          %7.2f %7.2f %7.2f ' %
                      (result['IoUs'][0], result['IoUs'][-1], result['mIoU']))
            
            wandb.log({'Epoch': epoch,
                    'Flow (EPE) loss': train_loss['flow'],
                    'Change (FE) loss': train_loss['change'],
                    'learning_late': scheduler.get_last_lr()[0],
                    'Train average loss': train_loss['total'],
                    'F1': result['f1'],
                    'Accuracy': result['accuracy'],
                    'IoU_static': result['IoUs'][0],
                    'IoU_change': result['IoUs'][-1],
                    'mIoU': result['mIoU']})

            # # Validation
            # result = \
            #     validate_epoch(args, model, val_dataloader, device, epoch=epoch,
            #                    save_path=os.path.join(save_path, 'val'),
            #                    writer = val_writer,
            #                    div_flow=args.div_flow,
            #                    loss_grid_weights=weights_loss_coeffs)
            # val_loss_grid, val_mean_epe, val_mean_epe_H_8, val_mean_epe_32, val_mean_epe_16  = \
            #     result['total'],result['mEPEs'][0].item(), result['mEPEs'][1].item(), result['mEPEs'][2].item(), result['mEPEs'][3].item()

            # print(colored('==> ', 'blue') + 'Val average grid loss :',
            #       val_loss_grid)
            # print('mean EPE is {}'.format(val_mean_epe))
            # print('mean EPE from reso H/8 is {}'.format(val_mean_epe_H_8))
            # print('mean EPE from reso 32 is {}'.format(val_mean_epe_32))
            # print('mean EPE from reso 16 is {}'.format(val_mean_epe_16))
            # val_writer.add_scalar('validation images: mean EPE ', val_mean_epe, epoch)
            # val_writer.add_scalar('validation images: mean EPE_from_reso_H_8', val_mean_epe_H_8, epoch)
            # val_writer.add_scalar('validation images: mean EPE_from_reso_32', val_mean_epe_32, epoch)
            # val_writer.add_scalar('validation images: mean EPE_from_reso_16', val_mean_epe_16, epoch)
            # val_writer.add_scalar('validation images: val loss', val_loss_grid, epoch)

            # print('          F1: {:.2f}, Accuracy: {:.2f} '.format(result['f1'], result['accuracy']))
            # print('          Static  |   Change   |   mIoU ')
            # print('          %7.2f %7.2f %7.2f ' %
            #       (result['IoUs'][0], result['IoUs'][-1], result['mIoU']))
            # print(colored('==> ', 'blue') + 'finished epoch :', epoch + 1)

            # # save checkpoint for each epoch and a fine called best_model so far
            # is_best = result['f1'] < best_val
            # best_val = min(result['f1'], best_val)
            # save_checkpoint({'epoch': epoch + 1,
            #                  'state_dict': model.module.state_dict(),
            #                  'optimizer': optimizer.state_dict(),
            #                  'scheduler': scheduler.state_dict(),
            #                  'best_loss': best_val},
            #                 is_best, save_path, 'epoch_{}.pth'.format(epoch + 1))

        print(args.seed, 'Training took:', time.time()-train_started, 'seconds')

    else:
        dec_rate = {}
        ref_miou = 0
        for dataset_name, test_dataloader in test_dataloaders.items():
            result = test_epoch(args, model, test_dataloader, device, epoch=start_epoch,
                                save_path=os.path.join('./visualize', dataset_name),
                                writer=val_writer,
                                plot_interval=args.plot_interval)
            # print('          F1: {:.2f}, Accuracy: {:.2f} '.format(result['f1'], result['accuracy']))
            print(f'         Environment: {dataset_name}')
            print('          Static  |   Change   |   mIoU ')
            print('          %7.2f  %7.2f  %7.2f ' %
                  (result['IoUs'][0], result['IoUs'][-1], result['mIoU']))

            if dataset_name == 'tunnel_normal':
                ref_miou = result['mIoU']
            else:
                dec_rate[dataset_name] = 100 * (ref_miou - result['mIoU']) / ref_miou
                print(f"          The decreasing rate of mIoU = {dec_rate[dataset_name]:.2f}%")
        
        avg_dec_rate = sum(dec_rate.values()) / len(dec_rate)
        print(f"          The average mIoU decreasing rate = {avg_dec_rate:.2f}%")



if __name__ == "__main__":
    torch.cuda.empty_cache()
    # Argument parsing
    parser = argparse.ArgumentParser(description='GLU-Net train script')
    # Paths
    parser.add_argument('--name_exp', type=str,
                        default='ai28',
                        help='name of the experiment to save')
    parser.add_argument('--training_data_dir', type=str, default='../../E2EChangeDet/dataset/train_datasets',
                        help='path to directory containing original images for training')
    parser.add_argument('--evaluation_data_dir', type=str,  default='./',
                        help='path to directory containing original images for validation')
    parser.add_argument('--snapshots', type=str, default='./snapshots')
    parser.add_argument('--pretrained', dest='pretrained',
                        default="./snapshots/sup_baseline_cs/epoch_19.pth",
                        help='path to pre-trained model (load only model params)')
    parser.add_argument('--resume', dest='resume',
                       default='',
                       help='path to resume model (load both model and optimizer params')
    parser.add_argument('--multi_class', action='store_true',
                        help='if true, do multi-class change detection')
    parser.add_argument('--sup_trainset_list', nargs='+')
    parser.add_argument('--usl_trainset_list', nargs='+')
    parser.add_argument('--testset_list', nargs='+', default=['tunnel_normal', 'tunnel_dust', 'tunnel_dark', 'tunnel_fire'])
    parser.add_argument('--valset_list', nargs='+', default=['synthetic'])
    parser.add_argument('--use_pac', action='store_true',
                        help='if true, do pixel-adaptive convolution when decoding')

    # Optimization parameters
    parser.add_argument('--lr', type=float, default=0.0001, help='learning rate')
    parser.add_argument('--momentum', type=float,
                        default=4e-4, help='momentum constant')
    parser.add_argument('--start_epoch', type=int, default=-1,
                        help='start epoch')
    parser.add_argument('--n_epoch', type=int, default=25,
                        help='number of training epochs')
    parser.add_argument('--batch_size', type=int, default=24, # for RTX3090
                        help='train/val batch size')
    parser.add_argument('--test_batch_size', type=int, default=1, # for RTX3090
                        help='test batch size')
    parser.add_argument('--n_threads', type=int, default=4,
                        help='number of parallel threads for dataloaders')
    parser.add_argument('--weight-decay', type=float, default=4e-4,
                        help='weight decay constant')
    parser.add_argument('--div_flow', type=float, default=1.0,
                        help='div flow')
    parser.add_argument('--seed', type=int, default=1986,
                        help='Pseudo-RNG seed')
    parser.add_argument('--split_ratio', type=float, default=0.1,
                        help='train/val split ratio')
    parser.add_argument('--split2_ratio', type=float, default=0.0001,
                        help='val/not-used split ratio (if 0.9, use 90% of val samples)')
    parser.add_argument('--plot_interval', type=int, default=1,
                        help='plot every N iteration')
    parser.add_argument('--test_interval', type=int, default=10,
                        help='test every N epoch')
    parser.add_argument('--milestones', nargs='+', type=int,
                        default=[7, 12], # for 10 epoch
                        help='schedule for learning rate decrease')
    parser.add_argument('--optim', type=str, default='adamw',
                        help='adam or adamw')
    parser.add_argument('--scheduler', type=str, default='multistep',
                        help='lambda or multistep or cosine_anneal')
    parser.add_argument('--train_img_size', nargs='+', type=int,
                        default=[520,520],
                        help='img_size (if you want to use synthetic dataset, this value should be (520,520)')
    parser.add_argument('--test_img_size', nargs='+', type=int,
                        default=[520,520],
                        help='img_size (if you want to use synthetic dataset, this value should be (520,520)')
    parser.add_argument('--disable_transform', action='store_true',
                        help='if true, do not perform transform when training')
    parser.add_argument('--img_norm_type',type=str, default='z_score',
                        help='z_score or min_max')
    parser.add_argument('--rgb_order', type=str, default='rgb',
                        help='rgb or bgr')
    parser.add_argument('--test_only', type=bool, default=True,
                        help='if true, do test only')
    parser.add_argument('--vpr_candidates', action='store_true',
                        help='if true, candidates of ref images are used (20 imgs)')
    parser.add_argument('--vpr_patchnetvlad', action='store_true',
                        help='if true, patch-netvlad based ref image is used')
    parser.add_argument('--vpr_netvlad', action='store_true',
                        help='if true, netvlad based ref image is used')
    parser.add_argument('--pyramid_type', type=str, default='ResNet',
                        help='VGG or ResNet')
    parser.add_argument('--cl_ptr_w', action='store_true',
                        help='if true, pretrained weights from DenseCL are loaded, only for ResNet')
    parser.add_argument('--cl', type=int, default=0,
                        help='if other than 0, multi-scale contrastive loss is added')
    parser.add_argument('--adap_coef', action='store_true',
                        help='if true, adaptive coefficients for loss_change, loss_cl')
    parser.add_argument('--except_occ', type=str, default="",
                        help='if "gt" or "prediction", \
                            occluded regions are excluded for computing loss_flow using "gt" or "predicted" mask')
    parser.add_argument('--tr_type', type=str, default='sup',
                        help='training type: supervised (sup), unsupervised (usl), and semi-supervsied (semi)')
    parser.add_argument('--s_sl', action='store_true',
                        help='if true, use semi-supervised flow and change loss')
    parser.add_argument('--not_logging', action='store_true',
                        help='if true, not log info. using wandb')
    parser.add_argument('--usl_frac', type=float, default=1.0,
                        help='Fraction rate of USL dataset')
    parser.add_argument('--sup_frac', type=float, default=1.0,
                        help='Fraction rate of Sup dataset')
    parser.add_argument('--run_name', type=str, default='',
                        help='Description of the setting')
    parser.add_argument('--l_f', type=float, default=1.0,
                        help='Coefficient for Feature Sim. Loss')
    parser.add_argument('--l_cr', type=float, default=0.1,
                        help='Coefficient for Change Regularization')
    parser.add_argument('--l_sm', type=float, default=2.0,
                        help='Coeeficient for Smoothness Regularization')
    parser.add_argument('--l_s_sup', type=float, default=1.0,
                        help='Weight for supervised loss of student')
    parser.add_argument('--l_s_semi', type=float, default=1.0,
                        help='Weight for consistency regularization')
    parser.add_argument('--f_alpha', type=float, default=1.0,
                        help='Multiplication factor for feature similarity')
    parser.add_argument('--f_beta', type=float, default=0.0,
                        help='Bias for feature similarity')
    parser.add_argument('--t_param', type=str, default='separate',
                        help='Setting for the parameters of the teacher net, option: separate, ema, share, fixed')
    parser.add_argument('--sweep', action='store_true',
                        help='Search hyperparameters')
    parser.add_argument('--make_vid', action='store_true',
                        help='Visualization for real world test samples')
    global gargs
    gargs = parser.parse_args()
    
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    
    print('device:{}'.format(device))
    print('-----------------------Arguments-----------------------------')
    for arg in vars(gargs):
        print('{}:{}'.format(arg, getattr(gargs, arg)))
    print('-------------------------------------------------------------')
    
    if gargs.sweep and not gargs.test_only and not gargs.not_logging:
        project = 'SCD_usl'
        sweep_configuration = {
                                    'method': 'random',
                                    'metric': {'goal': 'maximize', 'name': 'F1'},
                                    'parameters': 
                                    {
                                        'l_cr': {'max': 0.25, 'min': 0.01},
                                        'l_sm': {'max': 1.0, 'min': 0.0},
                                        # 'l_f': {'max':1.25, 'min': 0.75},
                                        # 'l_s_sup': {'max':1.5, 'min': 0.5},
                                        # 'l_s_semi': {'max':1.5, 'min': 1.0},
                                        # 'seed': {'max': 99999, 'min': 31}
                                    }
                                }
        sweep_id = wandb.sweep(sweep=sweep_configuration, project=project)
        wandb.agent(sweep_id=sweep_id, function=train_simsac, count=30)
    else:
        t0 = time.time()
        train_simsac()
        print(f'Elapsed time: {time.time() - t0:.2f} sec.')


