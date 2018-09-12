import os,sys

import numpy as np
from scipy.interpolate import interp1d
from scipy.integrate import quad as scipy_int1d
from scipy.special import jn, jn_zeros
from wigner_functions import *


d2r=np.pi/180.
sky_area=np.pi*4/(d2r)**2 #in degrees


class Covariance_utils():
    def __init__(self,f_sky=0,l=None,logger=None,l_cut_jnu=None,do_sample_variance=True,use_window=True,window_l=None,window_file=None,wig_3j=None):
        self.logger=logger
        self.l=l
        self.window_l=window_l
        self.window_file=window_file
        self.l_cut_jnu=l_cut_jnu #this is needed for hankel_transform case for xi. Need separate sigma_window calc.
        self.f_sky=f_sky

        self.use_window=use_window
        self.wig_3j=wig_3j
        self.sample_variance_f=1
        if not do_sample_variance:
            self.sample_variance_f=0 #remove sample_variance from gaussian part

        self.set_window_params(f_sky=self.f_sky)
        self.window_func(theta_win=self.theta_win,f_sky=self.f_sky)

        self.gaussian_cov_norm=(2.*l+1.)*f_sky*np.gradient(l) #need Delta l here. Even when
                                                                    #binning later

    def set_window_params(self,f_sky=None):
        self.theta_win=np.sqrt(f_sky*sky_area)
        if self.use_window:
            self.window_func()
            if self.wig_3j is None:
                m_1=0#FIXME: Use proper spins (m_i) here
                m_2=0
                self.wig_3j=Wigner3j_parallel( m_1, m_2, 0, self.l, self.l, self.window_l) 
            self.coupling_M=np.dot(self.wig_3j**2,self.Win*(2*self.window_l+1))
        else:
            self.Win=np.zeros_like(self.l,dtype='float32')
            x=self.l==0
            self.Win[x]=1.
        self.Om_W=4*np.pi*f_sky
        self.Win/=self.Om_W #FIXME: This thing has been forgotten and not used anywhere in the code.

    def window_func(self):
        if self.window_file is not None:
            self.window_l,self.Win=np.genfromtxt(self.window_file)
            return 
        
        if self.window_l is None:
            self.window_l=np.arange(100)+1
            
        l=self.window_l    
        theta_win=self.theta_win*d2r
        l_th=l*theta_win
        W=2*jn(1,l_th)/l_th*4*np.pi*self.f_sky
        return 

    def sigma_win_calc(self,cls_lin):
        if self.l_cut_jnu is None:
            self.sigma_win=np.dot(self.Win**2*np.gradient(self.l)*self.l,cls_lin.T)
        else: #FIXME: This is ugly. Only needed for hankel transform (not wigner). Remove if HT is deprecated.
            self.sigma_win={}
            for m1_m2 in self.l_cut_jnu['m1_m2s']:
                lc=self.l_cut_jnu[m1_m2]
                self.sigma_win[m1_m2]=np.dot(self.Win[lc]**2*np.gradient(self.l[lc])*self.l[lc],cls_lin[:,lc].T)
        #FIXME: This is ugly

    def corr_matrix(self,cov=[]):
        diag=np.diag(cov)
        return cov/np.sqrt(np.outer(diag,diag))


    def gaussian_cov_auto(self,cls,SN,tracers,z_indx,do_xi):
        """
        This is 'auto' covariance for a particular power spectra, but the power spectra
        itself could a cross-correlation, eg. galaxy-lensing cross correlations.
        For auto correlation, eg. lensing-lensing, cls1,cls2,cl12 should be same. Same for shot noise
        SN.

        """
        # print(cls[(tracers[0],tracers[2])].keys())
        G1324= ( cls[(tracers[0],tracers[2])] [(z_indx[0], z_indx[2]) ]*self.sample_variance_f
             # + (SN.get((tracers[0],tracers[2]))[:,z_indx[0], z_indx[2] ]  or 0)
             + (SN[(tracers[0],tracers[2])][:,z_indx[0], z_indx[2] ] if SN.get((tracers[0],tracers[2])) is not None else 0)
                )
             #get returns None if key doesnot exist. or 0 adds 0 is SN is none

        G1324*=( cls[(tracers[1],tracers[3])][(z_indx[1], z_indx[3]) ]*self.sample_variance_f
              # +(SN.get((tracers[1],tracers[3]))[:,z_indx[1], z_indx[3] ] or 0)
              + (SN[(tracers[1],tracers[3])][:,z_indx[1], z_indx[3] ] if SN.get((tracers[1],tracers[3])) is not None else 0)
              )

        G1423= ( cls[(tracers[0],tracers[3])][(z_indx[0], z_indx[3]) ]*self.sample_variance_f
              # + (SN.get((tracers[0],tracers[3]))[:,z_indx[0], z_indx[3] ] or 0)
              + (SN[(tracers[0],tracers[3])][:,z_indx[0], z_indx[3] ] if SN.get((tracers[0],tracers[3])) is not None else 0)
              )

        G1423*=( cls[(tracers[1],tracers[2])][(z_indx[1], z_indx[2]) ]*self.sample_variance_f
             # + (SN.get((tracers[1],tracers[2]))[:,z_indx[1], z_indx[2] ] or 0)
             + (SN[(tracers[1],tracers[2])][:,z_indx[1], z_indx[2] ] if SN.get((tracers[1],tracers[2])) is not None else 0)
                )

        G=None
        if not do_xi:
            G=np.diag(G1423+G1324)
            G/=self.gaussian_cov_norm
        return G,G1324,G1423
