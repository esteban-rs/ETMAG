from utils import psnr_and_ssim_torch
from model import Vgg19

import os
import numpy as np
import torch 
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torchvision.utils as utils
from imageio import imread, imsave

class Trainer():
    def __init__(self, args, logger, dataloader, model, loss_all):
        self.args       = args
        self.logger     = logger
        self.dataloader = dataloader
        self.model      = model
        self.loss_all   = loss_all
        self.device     = torch.device('cpu') if args.cpu else torch.device('cuda')
        self.vgg19      = Vgg19.Vgg19(requires_grad=False).to(self.device)
        
        self.params = [
            {"params": filter(lambda p: p.requires_grad, self.model.FE.parameters()), "lr": args.lr_rate_lte},
            {"params": filter(lambda p: p.requires_grad, self.model.CSFI.parameters()), "lr": args.lr_rate},        
            {"params": filter(lambda p: p.requires_grad, self.model.GDE.parameters()), "lr": args.lr_rate},  
        ]
        self.optimizer = optim.Adam(self.params, betas=(args.beta1, args.beta2), eps=args.eps)
        self.scheduler = optim.lr_scheduler.StepLR(self.optimizer, 
                                                   step_size = self.args.decay, 
                                                   gamma     = self.args.gamma)
        self.max_psnr       = 0.
        self.max_psnr_epoch = 0
        self.max_ssim       = 0.
        self.max_ssim_epoch = 0

    def load(self, model_path = None):
        if (model_path):
            self.logger.info('load_model_path: ' + model_path)
            model_state_dict_save = {k:v for k,v in torch.load(model_path, map_location=self.device).items()}
            model_state_dict      = self.model.state_dict()
            model_state_dict.update(model_state_dict_save)
            self.model.load_state_dict(model_state_dict, strict = False)

    def prepare(self, sample_batched):
        for key in sample_batched.keys():
            sample_batched[key] = sample_batched[key].to(self.device)
        return sample_batched

    def train(self, current_epoch=0, is_init=False):
        self.model.train()
        self.scheduler.step()
        self.logger.info('Current epoch learning rate: %e' %(self.optimizer.param_groups[0]['lr']))

        for i_batch, sample_batched in enumerate(self.dataloader['train']):
            self.optimizer.zero_grad()

            sample_batched = self.prepare(sample_batched)
            lr             = sample_batched['LR']
            lr_sr          = sample_batched['LR_sr']
            hr             = sample_batched['HR']
            ref            = sample_batched['Ref']
            ref_sr         = sample_batched['Ref_sr']

            sr, S    = self.model(lr    = lr, lrsr  = lr_sr, ref   = ref, refsr = ref_sr)
            # calc loss
            is_print = ((i_batch + 1) % self.args.print_every == 0) 

            rec_loss = self.args.rec_w * self.loss_all['rec_loss'](sr, hr)
            loss     = rec_loss
            if (is_print):
                self.logger.info( ('init ' if is_init else '') + 'epoch: ' + str(current_epoch) + 
                    '\t batch: ' + str(i_batch+1) )
                self.logger.info( 'rec_loss: %.10f' %(rec_loss.item()) )
                
            if (not is_init):
                if ('per_loss' in self.loss_all):
                    sr_relu5_1 = self.vgg19((sr + 1.) / 2.)
                    with torch.no_grad():
                        hr_relu5_1 = self.vgg19((hr.detach() + 1.) / 2.)
                    per_loss = self.args.per_w * self.loss_all['per_loss'](sr_relu5_1, hr_relu5_1)
                    loss += per_loss
                    if (is_print):
                        self.logger.info( 'per_loss: %.10f' %(per_loss.item()) )
                if ('grd_loss' in self.loss_all):
                    grd_loss = self.args.grd_w * self.loss_all['grd_loss'](sr, hr)
                    loss += grd_loss
                    if (is_print):
                        self.logger.info( 'grd_loss: %.10f' %(grd_loss.item()) )
                if ('adv_loss' in self.loss_all):
                    adv_loss = self.args.adv_w * self.loss_all['adv_loss'](sr, hr)
                    loss += adv_loss
                    if (is_print):
                        self.logger.info( 'adv_loss: %.10f' %(adv_loss.item()) )
                        
            loss.backward()
            self.optimizer.step()

        if current_epoch % self.args.save_every == 0 :
            self.logger.info('saving the model...')
            tmp = self.model.state_dict()
            model_state_dict = {key.replace('module.',''): tmp[key] for key in tmp if 
                (('SearchNet' not in key) and ('_copy' not in key))}
            model_name = self.args.save_dir.strip('/')+'/model/model_'+str(current_epoch).zfill(5)+'.pt'
            torch.save(model_state_dict, model_name)
            
    def evaluate(self, current_epoch=0):
        self.logger.info('Epoch ' + str(current_epoch) + ' evaluation process...')

        if (self.args.dataset == 'CUFED'):
            self.model.eval()
            with torch.no_grad():
                psnr, ssim, cnt = 0., 0., 0
                for i_batch, sample_batched in enumerate(self.dataloader['test']['1']):
                    cnt += 1
                    sample_batched = self.prepare(sample_batched)
                    lr     = sample_batched['LR']
                    lr_sr  = sample_batched['LR_sr']
                    hr     = sample_batched['HR']
                    ref    = sample_batched['Ref']
                    ref_sr = sample_batched['Ref_sr']

                    sr, _  = self.model(lr=lr, lrsr=lr_sr, ref=ref, refsr=ref_sr)
                    if (self.args.eval_save_results):
                        sr_save = (sr+1.) * 127.5
                        sr_save = np.transpose(sr_save.squeeze().round().cpu().numpy(), (1, 2, 0)).astype(np.uint8)
                        imsave(os.path.join(self.args.save_dir, 'save_results', str(i_batch).zfill(5)+'.png'), sr_save)
                    
                    ### calculate psnr and ssim
                    _psnr, _ssim = psnr_and_ssim_torch.calc_psnr_and_ssim(sr.detach(), hr.detach())

                    psnr += _psnr
                    ssim += _ssim

                psnr_ave = psnr / cnt
                ssim_ave = ssim / cnt
                self.logger.info('Ref  PSNR (now): %.3f \t SSIM (now): %.4f' %(psnr_ave, ssim_ave))
                if (psnr_ave > self.max_psnr):
                    self.max_psnr = psnr_ave
                    self.max_psnr_epoch = current_epoch
                if (ssim_ave > self.max_ssim):
                    self.max_ssim = ssim_ave
                    self.max_ssim_epoch = current_epoch
                self.logger.info('Ref  PSNR (max): %.3f (%d) \t SSIM (max): %.4f (%d)' 
                    %(self.max_psnr, self.max_psnr_epoch, self.max_ssim, self.max_ssim_epoch))
        
        else :
            self.model.eval()
            with torch.no_grad():
                psnr, ssim, cnt = 0., 0., 0
                for i_batch, sample_batched in enumerate(self.dataloader['test']):
                    cnt += 1
                    sample_batched = self.prepare(sample_batched)
                    lr     = sample_batched['LR']
                    lr_sr  = sample_batched['LR_sr']
                    hr     = sample_batched['HR']
                    ref    = sample_batched['Ref']
                    ref_sr = sample_batched['Ref_sr']

                    sr, _, _, _, _ = self.model(lr=lr, lrsr=lr_sr, ref=ref, refsr=ref_sr)
                    if (self.args.eval_save_results):
                        sr_save = (sr+1.) * 127.5
                        sr_save = np.transpose(sr_save.squeeze().round().cpu().numpy(), (1, 2, 0)).astype(np.uint8)
                        imsave(os.path.join(self.args.save_dir, 'save_results', str(i_batch).zfill(5)+'.png'), sr_save)
                    
                    ### calculate psnr and ssim
                    _psnr, _ssim = psnr_and_ssim_torch.calc_psnr_and_ssim(sr.detach(), hr.detach())

                    psnr += _psnr
                    ssim += _ssim

                psnr_ave = psnr / cnt
                ssim_ave = ssim / cnt
                self.logger.info('Ref  PSNR (now): %.3f \t SSIM (now): %.4f' %(psnr_ave, ssim_ave))
                if (psnr_ave > self.max_psnr):
                    self.max_psnr = psnr_ave
                    self.max_psnr_epoch = current_epoch
                if (ssim_ave > self.max_ssim):
                    self.max_ssim = ssim_ave
                    self.max_ssim_epoch = current_epoch
                self.logger.info('Ref  PSNR (max): %.3f (%d) \t SSIM (max): %.4f (%d)' 
                    %(self.max_psnr, self.max_psnr_epoch, self.max_ssim, self.max_ssim_epoch))

        self.logger.info('Evaluation over.')

 