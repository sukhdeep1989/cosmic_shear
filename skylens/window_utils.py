        #TODO: 
        # - Allow windows to be read from a file.
        # - Allow an alternative graph that allows different windows to be computed independently (i.e. wigner split is secondary). 
        
import dask
from dask import delayed
import sparse
from skylens.wigner_transform import *
from skylens.binning import *
from skylens.cov_utils import *
import numpy as np
import healpy as hp
from scipy.interpolate import interp1d
import warnings,logging
from distributed import LocalCluster
from dask.distributed import Client,get_client
import h5py
import zarr
from dask.threaded import get
import time,gc
from multiprocessing import Pool,cpu_count
from skylens.thread_count import *
import pickle


class window_utils():
    def __init__(self,window_l=None,window_lmax=None,l=None,l_bins=None,corrs=None,s1_s2s=None,use_window=None,f_sky=None,
                do_cov=False,cov_utils=None,corr_indxs=None,z_bins=None,WT=None,xi_bin_utils=None,do_xi=False,
                store_win=False,Win=None,wigner_files=None,step=None,xi_win_approx=False,
                cov_indxs=None,client=None,scheduler_info=None,wigner_step=None,
                kappa_class0=None,kappa_class_b=None,bin_window=True,do_pseudo_cl=True):
        self.__dict__.update(locals()) #assign all input args to the class as properties
        self.binning=binning()
        
        nl=len(self.l)
        nwl=len(self.window_l)*1.0
        
        self.MF=(2*self.l[:,None]+1)# this is multiplied with coupling matrices, before binning.


        self.step=wigner_step
        if step is None:
            self.step=np.int32(100.*((2000./nl)**2)*(1000./nwl)) #small step is useful for lower memory load
            self.step=np.int32(min(self.step,nl+1))
#         self.step=50
            
        self.lms=np.int32(np.arange(nl,step=self.step))
        print('Win gen: step size',self.step,self.do_pseudo_cl,xi_win_approx,nl,nwl)#,self.lms)

        self.c_ell0=None
        self.c_ell_b=None
        del self.kappa_class0,self.kappa_class_b#,self.z_bins
        if bin_window:
            self.binnings=binning()
            self.cl_bin_utils=kappa_class0.cl_bin_utils
#             self.kappa_class0=kappa_class0
#             self.kappa_class_b=kappa_class_b

            self.c_ell0=kappa_class0.cl_tomo()['cl']
            if kappa_class_b is not None:
                self.c_ell_b=kappa_class_b.cl_tomo()['cl']
            else:
                self.c_ell_b=kappa_class0.cl_tomo()['cl_b']
                
        if self.Win is None and self.use_window and self.do_pseudo_cl: #pseudo_cl window
            if self.do_xi:
                print('Warning: window for xi is different from cl.')
            self.set_wig3j()
            if self.store_win:
                self.set_store_window(corrs=self.corrs,corr_indxs=self.corr_indxs,client=None)
            else:
                self.set_window_graph(corrs=self.corrs,corr_indxs=self.corr_indxs,client=None)
        elif self.do_xi and xi_win_approx:# and self.use_window:
            self.set_window_cl(corrs=corrs,corr_indxs=corr_indxs,client=client)
            self.Win=delayed(self.combine_coupling_xi_cov)(self.Win_cl,self.Win_cov)
            print('Got xi win graph',self.Win,self.Win_cl,self.Win_cov)
            if self.store_win:
                if client is None:
                    client=get_client(address=self.scheduler_info['address'])
                self.Win=client.compute(self.Win).result()
#             self.cleanup()

    def wig3j_step_read(self,m=0,lm=None):
        """
        wigner matrices are large. so we read them ste by step
        """
        step=self.step
        out=self.wig_3j[m].oindex[np.int32(self.window_l),np.int32(self.l[lm:lm+step]),np.int32(self.l)]
        out=out.transpose(1,2,0)
        return out

    def set_wig3j_step_multiplied(self,lm=None):
        """
        product of two partial migner matrices
        """
        print('getting wig3j',lm)
        wig_3j_2={}
        wig_3j_1={m1: self.wig3j_step_read(m=m1,lm=lm) for m1 in self.m_s}
        mi=0
        for m1 in self.m_s:
            for m2 in self.m_s[mi:]:
                wig_3j_2[str(m1)+str(m2)]=wig_3j_1[m1]*wig_3j_1[m2].astype('float64') #numpy dot appears to run faster with 64bit ... ????
            mi+=1
        del wig_3j_1
        return wig_3j_2

    def set_wig3j_step_spin(self,wig2,mf_pm,W_pm):
        """
        wig2 is product of two wigner matrices. Here multply with the spin dependent factors
        """
        if W_pm==2: #W_+
            mf=mf_pm['mf_p']#.astype('float64') #https://stackoverflow.com/questions/45479363/numpy-multiplying-large-arrays-with-dtype-int8-is-slow
        if W_pm==-2: #W_+
            mf=1-mf_pm['mf_p']
        return wig2*mf

    def set_window_pm_step(self,lm=None):
        """
        Here we set the spin dependent multiplicative factors (X+, X-).
        """
        li1=np.int32(self.window_l).reshape(len(self.window_l),1,1)
        li3=np.int32(self.l).reshape(1,1,len(self.l))
        li2=np.int32(self.l[lm:lm+self.step]).reshape(1,len(self.l[lm:lm+self.step]),1)
        mf=(-1.)**(li1+li2+li3)
        mf=mf.transpose(1,2,0)
        out={}
        out['mf_p']=(1.+mf)/2.
        # out['mf_p']=np.int8((1.+mf)/2.)#.astype('bool')
                              #bool doesn't help in itself, as it is also byte size in numpy.
                              #we donot need to store mf_n, as it is simply a 0-1 flip or "not" when written as bool
                              #using bool or int does cost somewhat in computation as numpy only computes with float 64 (or 32 
                              #in 32 bit systems). If memory is not an
                              #issue, use float64 here and then use mf_n=1-mf_p.
#         del mf
        return out

    def set_wig3j(self):
        """
        Set up a graph (dask delayed), where nodes to read in partial wigner matrices, get their products and 
        also the spin depednent multiplicative factors.
        """
        self.wig_3j={}
        if not self.use_window:
            return

        m_s=np.concatenate([np.abs(i).flatten() for i in self.s1_s2s.values()])
        self.m_s=np.sort(np.unique(m_s))

        if self.wigner_files is None:
            self.wigner_files={}
            self.wigner_files[0]= 'temp/dask_wig3j_l6500_w1100_0_reorder.zarr'
            self.wigner_files[2]= 'temp/dask_wig3j_l6500_w1100_2_reorder.zarr'

        print('wigner_files:',self.wigner_files)

        for m in self.m_s:
            self.wig_3j[m]=zarr.open(self.wigner_files[m],mode='r')

        self.wig_3j_2={}
        self.wig_3j_1={}
        self.mf_pm={}

        for lm in self.lms:
            self.wig_3j_2[lm]=delayed(self.set_wig3j_step_multiplied)(lm=lm)
            self.mf_pm[lm]=delayed(self.set_window_pm_step)(lm=lm)
            
        self.wig_s1s2s={}
        for corr in self.corrs:
            mi=np.sort(np.absolute(self.s1_s2s[corr]).flatten())
            self.wig_s1s2s[corr]=str(mi[0])+str(mi[1])
        print('wigner done',self.wig_3j.keys())


    # def coupling_matrix(self,win,wig_3j_1,wig_3j_2,W_pm=0):
    #     """
    #     get the coupling matrix from windows power spectra and wigner functions. Not used
    #     as we now use the coupling_matrix_large by default.
    #     """
    #     return np.dot(wig_3j_1*wig_3j_2,win*(2*self.window_l+1)   )/4./np.pi

    def coupling_matrix_large(self,win,wig_3j_2,mf_pm,bin_wt,W_pm,lm,cov):
        """
        get the large coupling matrices from windows power spectra, wigner functions and spin dependent 
        multiplicative factors. Also do the binning if called for. 
        This function supports on partial matrices.
        """
        wig=wig_3j_2 #[W_pm]
        if W_pm!=0:
            if W_pm==2: #W_+
                wig=wig*mf_pm['mf_p']#.astype('float64') #https://stackoverflow.com/questions/45479363/numpy-multiplying-large-arrays-with-dtype-int8-is-slow
            if W_pm==-2: #W_-
                wig=wig*(1-mf_pm['mf_p'])

        M={}
        for k in win.keys():
            M[k]=wig@(win[k]*(2*self.window_l+1))
            M[k]/=4.*np.pi
            if not cov:
                M[k]=M[k]*self.MF.T #FIXME: not used in covariance?
            if self.bin_window:# and bin_wt is not None:
                M[k]=self.binnings.bin_2d_coupling(M=M[k],bin_utils=self.cl_bin_utils,
                    partial_bin_side=2,lm=lm,lm_step=self.step,wt0=bin_wt['wt0'],wt_b=bin_wt['wt_b'],cov=cov)
                
        if W_pm!=0:
            del wig
        return M

    def multiply_window(self,win1,win2):
        """
        Take product of two windows which maybe partially overlapping and mask it properly.
        """
        W=win1*win2
        x=np.logical_or(win1==hp.UNSEEN, win2==hp.UNSEEN)
        W[x]=hp.UNSEEN
        return W

    def mask_comb(self,win1,win2): 
        """
        combined the mask from two windows which maybe partially overlapping.
        Useful for some covariance calculations, specially SSC, where we assume a uniform window.
        """
        W=win1*win2
        x=np.logical_or(win1==hp.UNSEEN, win2==hp.UNSEEN)
        W[x]=hp.UNSEEN
        W[~x]=1. #mask = 0,1
        fsky=(~x).mean()
        return fsky,W#.astype('int16')

    def get_window_power_cl(self,corr_indxs,c_ell0=None,c_ell_b=None):#corr={},indxs={}):
        """
        Get the cross power spectra of windows given two tracers.
        Note that noise and signal have different windows and power spectra for both 
        cases. 
        Spin factors and binning weights if needed are also set here.
        """
#         print('getting window power cl',corr_indxs)
        corr=(corr_indxs[0],corr_indxs[1])
        indxs=(corr_indxs[2],corr_indxs[3])
        win={}
        win['corr']=corr
        win['indxs']=indxs

        s1s2=np.absolute(self.s1_s2s[corr]).flatten()
        W_pm=0
        if np.sum(s1s2)!=0:
            W_pm=2 #we only deal with E mode\
            if corr==('shearB','shearB'):
                W_pm=-2
        if self.do_xi:
            W_pm=0 #for xi estimators, there is no +/-. Note that this will result in wrong thing for pseudo-C_ell.
                    #FIXME: hence pseudo-C_ell and xi together are not supported right now

        z_bin1=self.z_bins[corr[0]][indxs[0]]
        z_bin2=self.z_bins[corr[1]][indxs[1]]

        win[12]={} #to keep some naming uniformity with the covariance window
        win[12]['cl']=hp.anafast(map1=z_bin1['window'],map2=z_bin2['window'],
                                 lmax=self.window_lmax)[self.window_l]

        if corr[0]==corr[1] and indxs[0]==indxs[1]:
            map1=np.sqrt(z_bin1['window'])
            mask=z_bin1['window']==hp.UNSEEN
            map1[mask]=hp.UNSEEN
            win[12]['N']=hp.anafast(map1=map1,lmax=self.window_lmax)[self.window_l]

        win['binning_util']=None
        win['bin_wt']=None
        if self.bin_window:
            cl0=c_ell0[corr][indxs]
            cl_b=c_ell_b[corr][indxs]
            win['bin_wt']={'wt_b':1./cl_b,'wt0':cl0}
            if np.all(cl_b==0):#avoid nan
                win['bin_wt']={'wt_b':cl_b,'wt0':cl0}
            
        win['W_pm']=W_pm
        win['s1s2']=s1s2
        if self.do_xi:
            th,win['xi']=self.WT.projected_correlation(l_cl=self.window_l,s1_s2=(0,0),cl=win[12]['cl'])
            win['xi']=1+win['xi']
            win['xi_b']=self.binning.bin_1d(xi=win['xi'],bin_utils=self.xi_bin_utils[(0,0)])

        win['M']={} #self.coupling_matrix_large(win['cl'], s1s2,wig_3j_2=wig_3j_2,W_pm=W_pm)*(2*self.l[:,None]+1) #FIXME: check ordering
        win['M_noise']=None
        return win

    def get_cl_coupling_lm(self,corr_indxs,win,lm,wig_3j_2_lm,mf_pm):
        """
        This function gets the partial coupling matrix given window power spectra and wigner functions. 
        Note that it get the matrices for both signal and noise as well as for E/B modes if applicable.
        """
#         indx=self.cl_keys.index(corr_indxs)
        win0=win#[indx]
        win2={}
        i=0
        k=win0['corr']+win0['indxs']

        corr=(k[0],k[1])

        wig_3j_2=wig_3j_2_lm[self.wig_s1s2s[corr]]
        win=win0#[i]#[k]
#             assert win['corr']==corr
        win2={'M':{},'M_noise':None,'M_B':None,'M_B_noise':None,'binning_util':win['binning_util']}
        for kt in ['corr','indxs']:
            win2[kt]=win0[kt]
        if lm==0:
            win2=win

        win_M=self.coupling_matrix_large(win[12], wig_3j_2=wig_3j_2,mf_pm=mf_pm,bin_wt=win['bin_wt']
                                     ,W_pm=win['W_pm'],lm=lm,cov=False)
        win2['M'][lm]=win_M['cl']
        if 'N' in win_M.keys():
            win2['M_noise']={lm:win_M['N']}
        if win['corr']==('shear','shear') and win['indxs'][0]==win['indxs'][1]:# and not self.do_xi: #B-mode for cl
        #FIXME for xi, window is same in xi+/-. pseudo-C_ell and xi together are not supported right now.
            win_M=self.coupling_matrix_large(win[12],wig_3j_2=wig_3j_2,mf_pm=mf_pm,W_pm=-2,bin_wt=win['bin_wt'],
                                            lm=lm,cov=False)
            win2['M_B_noise']={lm: win_M['N']}
            win2['M_B']={lm: win_M['cl']}
        i+=1
        return win2
    
    def get_cl_coupling_all_lm(self,corr_indxs,win0,wig_3j_2,mf_pm):
        win_lm={}
        client=get_client(address=self.scheduler_info['address'])
        wig_3j_2=client.compute(wig_3j_2).result()
        mf_pm=client.compute(mf_pm).result()
        for lm in self.lms:
            win_lm[lm]=self.get_cl_coupling_lm(corr_indxs,win0,lm,wig_3j_2[lm],mf_pm[lm])
#             win_lm[lm]=delayed(self.get_cl_coupling_lm)(win0,wig_3j_2[lm],mf_pm[lm])
        return win_lm
    
    def combine_coupling_cl(self,result):
        """
        This function combines the partial coupling matrices computed above. It loops over all combinations of tracers
        and returns a dictionary of coupling matrices for all C_ells.
        """
        dic={}
        nl=len(self.l)
        # nl2=nl
        if self.bin_window:
            nl=len(self.l_bins)-1

        for ii_t in np.arange(len(self.cl_keys)): #list(result[0].keys()):
            ii=0#because we are deleting below
            
            result_ii=result[0][ii]
            corr=result_ii['corr']
            indxs=result_ii['indxs']

            result0={}
            for k in result_ii.keys():
                result0[k]=result_ii[k]

            result0['M']=np.zeros((nl,nl))
            if  result_ii['M_noise'] is not None:
                result0['M_noise']=np.zeros((nl,nl))
            if corr==('shear','shear') and indxs[0]==indxs[1]:
                result0['M_B_noise']=np.zeros((nl,nl))
                result0['M_B']=np.zeros((nl,nl))

            for i_lm in np.arange(len(self.lms)):
                lm=self.lms[i_lm]
                start_i=lm
                end_i=lm+self.step
                if self.bin_window:
                    start_i=0
                    end_i=nl
              
                result0['M'][start_i:end_i,:]+=result[lm][ii]['M'][lm]
                if  result_ii['M_noise'] is not None:
                    result0['M_noise'][start_i:end_i,:]+=result[lm][ii]['M_noise'][lm]
                if corr==('shear','shear') and indxs[0]==indxs[1]:
                    result0['M_B_noise'][start_i:end_i,:]+=result[lm][ii]['M_B_noise'][lm]
                    result0['M_B'][start_i:end_i,:]+=result[lm][ii]['M_B'][lm]

                del result[lm][ii]

            corr21=corr[::-1]
            if dic.get(corr) is None:
                dic[corr]={}
            if dic.get(corr21) is None:
                dic[corr21]={}
            dic[corr][indxs]=result0
            dic[corr[::-1]][indxs[::-1]]=result0
        return dic
    
    def combine_single_coupling_cl(self,corr_indxs,result):
        """
        This function combines the partial coupling matrices computed above. It loops over all lms for a 
        given correlation
        and returns a dictionary of coupling matrices for C_ell.
        """
        nl=len(self.l)
        # nl2=nl
        if self.bin_window:
            nl=len(self.l_bins)-1

        result_ii=result[0]
        corr=result_ii['corr']
        indxs=result_ii['indxs']

        result0={}
        
        for k in result_ii.keys():
            result0[k]=result_ii[k]

        result0['M']=np.zeros((nl,nl))
        if  result_ii['M_noise'] is not None:
            result0['M_noise']=np.zeros((nl,nl))
        if corr==('shear','shear') and indxs[0]==indxs[1]:
            result0['M_B_noise']=np.zeros((nl,nl))
            result0['M_B']=np.zeros((nl,nl))

        for i_lm in np.arange(len(self.lms)):
            lm=self.lms[i_lm]
            start_i=lm
            end_i=lm+self.step
            if self.bin_window:
                start_i=0
                end_i=nl
            
            result0['M'][start_i:end_i,:]+=result[lm]['M'][lm]
            if  result_ii['M_noise'] is not None:
                result0['M_noise'][start_i:end_i,:]+=result[lm]['M_noise'][lm]
            if corr==('shear','shear') and indxs[0]==indxs[1]:
                result0['M_B_noise'][start_i:end_i,:]+=result[lm]['M_B_noise'][lm]
                result0['M_B'][start_i:end_i,:]+=result[lm]['M_B'][lm]

            del result[lm]
          
#         corr21=corr[::-1]
#         dic={corr:{},corr21:{}}
        dic={corr+indxs:result0}
#         dic[corr21][indxs[::-1]]=result0
        return result0
    
    def combine_coupling_xi(self,result):
        dic={}
        #for corr in corrs:
         #   dic[corr]={}
          #  dic[corr[::-1]]={}
        i=0
        for ii in self.cl_keys: #list(result.keys()):
            result_ii=result[i]
            corr=result_ii['corr']
            indxs=result_ii['indxs']
            corr21=corr[::-1]
            if dic.get(corr) is None:
                dic[corr]={}
            if dic.get(corr21) is None:
                dic[corr21]={}
            dic[corr][indxs]=result_ii
            dic[corr[::-1]][indxs[::-1]]=result_ii
            i+=1
        return dic
    
    def cov_s1s2s(self,corr): 
        """
        Set the spin factors that will be used in window calculations for two different covariances.
        when spins are not same, we set them to 0. Expressions are not well defined in this case. Should be ok for l>~50 ish
        """
        s1s2=np.absolute(self.s1_s2s[corr]).flatten()
        if s1s2[0]==s1s2[1]:
            return s1s2[0]
        else:
            return 0

    def get_window_power_cov(self,corr_indxs,c_ell0=None,c_ell_b=None):#corr1=None,corr2=None,indxs1=None,indxs2=None):
        """
        Compute window power spectra what will be used in the covariance calculations. 
        For covariances, we have four windows. Pairs of them are first multiplied together and
        then a power spectra is computed. 
        Separate calculations are done for different combinations of tracers (13-24 and 14-23) which
        are further split into different combinations of noise and signal (signal-signal, noise-noise) 
        and noise-signal.
        """
#         print('getting window power cov',corr_indxs)
        corr1=(corr_indxs[0],corr_indxs[1])
        corr2=(corr_indxs[2],corr_indxs[3])
        indxs1=(corr_indxs[4],corr_indxs[5])
        indxs2=(corr_indxs[6],corr_indxs[7])
        win={}
        corr=corr1+corr2
        indxs=indxs1+indxs2
        win['corr1']=corr1
        win['corr2']=corr2
        win['indxs1']=indxs1
        win['indxs2']=indxs2
        win['corr_indxs']=corr_indxs

        def get_window_spins(cov_indxs=[(0,2),(1,3)]):    #W +/- factors based on spin
            W_pm=[0]
            if self.do_xi:
                return W_pm#for xi estimators, there is no +/-. Note that this will result in wrong thing for pseudo-C_ell.
                    #FIXME: hence pseudo-C_ell and xi together are not supported right now

            s=[np.sum(self.s1_s2s[corr1]),np.sum(self.s1_s2s[corr2])]

            if s[0]==2 and s[1]==2: #gE,gE
                W_pm=[2]
            elif 4 in s and 2 in s: #EE,gE
                W_pm=[2]
            elif 0 in s and 2 in s: #gg,gE
                W_pm=[2]
            elif 4 in s and 0 in s: #EE,gg
                W_pm=[2]
                for i in np.arange(2):
                    if indxs[cov_indxs[i][0]]==indxs[cov_indxs[i][1]] and s[i]==4: #auto correlation, include B modes
                        W_pm=[2,-2]
            elif s[0]==4 and s[1]==4: #EE,EE
                W_pm=[2]
                for i in np.arange(2):
                    if indxs[cov_indxs[i][0]]==indxs[cov_indxs[i][1]] and s[i]==4: #auto correlation, include B modes
                        W_pm=[2,-2]

            return W_pm


        s1s2s={}

        s1s2s[1324]=np.array([self.cov_s1s2s(corr=(corr[0],corr[2])), #13
                              self.cov_s1s2s(corr=(corr[1],corr[3])) #24
                              ])

        s1s2s[1423]=np.array([self.cov_s1s2s(corr=(corr[0],corr[3])), #14
                              self.cov_s1s2s(corr=(corr[1],corr[2])) #23
                            ])

        W_pm={} #W +/- factors based on spin
        W_pm[1324]=get_window_spins(cov_indxs=[(0,2),(1,3)])
        W_pm[1423]=get_window_spins(cov_indxs=[(0,3),(1,2)])

        z_bin1=self.z_bins[corr[0]][indxs[0]]
        z_bin2=self.z_bins[corr[1]][indxs[1]]
        z_bin3=self.z_bins[corr[2]][indxs[2]]
        z_bin4=self.z_bins[corr[3]][indxs[3]]

        win[1324]={}
        win[1423]={}
        win[1324]['clcl']=hp.anafast(map1=self.multiply_window(z_bin1['window'],z_bin3['window']),
                                 map2=self.multiply_window(z_bin2['window'],z_bin4['window']),
                                 lmax=self.window_lmax
                        )[self.window_l]

        if corr[0]==corr[2] and indxs[0]==indxs[2]: #noise X cl
            win[1324]['Ncl']=hp.anafast(map1=z_bin1['window'],
                                 map2=self.multiply_window(z_bin2['window'],z_bin4['window']),
                                 lmax=self.window_lmax
                        )[self.window_l]
        if corr[1]==corr[3] and indxs[1]==indxs[3]:#noise X cl
            win[1324]['clN']=hp.anafast(map1=self.multiply_window(z_bin1['window'],z_bin3['window']),
                                 map2=z_bin2['window'],
                                 lmax=self.window_lmax
                        )[self.window_l]
        if corr[0]==corr[2] and indxs[0]==indxs[2] and corr[1]==corr[3] and indxs[1]==indxs[3]: #noise X noise
            win[1324]['NN']=hp.anafast(map1=z_bin1['window'],
                                 map2=z_bin2['window'],
                                 lmax=self.window_lmax
                        )[self.window_l]

        win[1423]['clcl']=hp.anafast(map1=self.multiply_window(z_bin1['window'],z_bin4['window']),
                                 map2=self.multiply_window(z_bin2['window'],z_bin3['window']),
                                 lmax=self.window_lmax
                            )[self.window_l]

        if corr[0]==corr[3] and indxs[0]==indxs[3]: #noise14 X cl
            win[1423]['Ncl']=hp.anafast(map1=z_bin1['window'],
                                 map2=self.multiply_window(z_bin2['window'],z_bin3['window']),
                                 lmax=self.window_lmax
                        )[self.window_l]
        if corr[1]==corr[2] and indxs[1]==indxs[2]:#noise23 X cl
            win[1423]['clN']=hp.anafast(map1=self.multiply_window(z_bin1['window'],z_bin4['window']),
                                 map2=z_bin2['window'],
                                 lmax=self.window_lmax
                        )[self.window_l]
        if corr[0]==corr[3] and indxs[0]==indxs[3] and corr[1]==corr[2] and indxs[1]==indxs[2]: #noise X noise
            win[1423]['NN']=hp.anafast(map1=z_bin1['window'],
                                 map2=z_bin2['window'],
                                 lmax=self.window_lmax
                        )[self.window_l]


        win['binning_util']=None
        win['bin_wt']=None
        if self.bin_window:  #FIXME: this will be used to get an approximation, because we donot save unbinned covariance
            win['bin_wt']={}
            win['bin_wt']['cl13']=c_ell0[(corr[0],corr[2])][(indxs[0],indxs[2])]
            win['bin_wt']['cl24']=c_ell0[(corr[1],corr[3])][(indxs[1],indxs[3])] 
            win['bin_wt']['cl14']=c_ell0[(corr[0],corr[3])][(indxs[0],indxs[3])]
            win['bin_wt']['cl23']=c_ell0[(corr[1],corr[2])][(indxs[1],indxs[2])] 

            win['bin_wt']['cl_b13']=c_ell_b[(corr[0],corr[2])][(indxs[0],indxs[2])]
            win['bin_wt']['cl_b24']=c_ell_b[(corr[1],corr[3])][(indxs[1],indxs[3])] 
            win['bin_wt']['cl_b14']=c_ell_b[(corr[0],corr[3])][(indxs[0],indxs[3])]
            win['bin_wt']['cl_b23']=c_ell_b[(corr[1],corr[2])][(indxs[1],indxs[2])] 
            
        win['f_sky12'],mask12=self.mask_comb(z_bin1['window'],z_bin2['window'],
                                     )#For SSC
        win['f_sky34'],mask34=self.mask_comb(
                                z_bin3['window'],z_bin4['window']
                                     )
        win['mask_comb_cl']=hp.anafast(map1=mask12,
                                 map2=mask34,
                                 lmax=self.window_lmax
                            ) #based on 4.34 of https://arxiv.org/pdf/1711.07467.pdf
        win['Om_w12']=win['f_sky12']*4*np.pi
        win['Om_w34']=win['f_sky34']*4*np.pi
        del mask12,mask34
        win['M']={1324:{},1423:{}}

        for k in win[1324].keys():
            win['M'][1324][k]={wp:{} for wp in W_pm[1324]}
        for k in win[1423].keys():
            win['M'][1423][k]={wp:{} for wp in W_pm[1423]}

        win['xi']={1324:{},1423:{}}
        win['xi_b']={1324:{},1423:{}}
        if self.do_xi:
            for k in win[1324].keys():
                th,win['xi'][1324][k]=self.WT.projected_covariance(l_cl=self.window_l,s1_s2=(0,0),cl_cov=win[1324][k])
                win['xi_b'][1324][k]=self.binning.bin_2d(cov=win['xi'][1324][k],bin_utils=self.xi_bin_utils[(0,0)])
            for k in win[1423].keys():
                th,win['xi'][1423][k]=self.WT.projected_covariance(l_cl=self.window_l,s1_s2=(0,0),cl_cov=win[1423][k])
                win['xi_b'][1423][k]=self.binning.bin_2d(cov=win['xi'][1423][k],bin_utils=self.xi_bin_utils[(0,0)])

        win['W_pm']=W_pm
        win['s1s2']=s1s2s
        return win

    def get_cov_coupling_lm(self,corr_indxs,win_all,lm,wig_3j_2,mf_pm):
        """
        This function computes the partial coupling matrix for a given covariance matrix between two C_ells.
        Requires window power spectra and wigner functions in the input, which are different for different
        elements of covariance. Here by different elements we mean two parts of covariance, 13-24 and 14-23, which
        are further split buy different combinations of noise and signal power spectra.
        """
#         indx=self.cov_keys.index(corr_indxs)
        win0=win_all#[indx]

        i=0
#         for k0 in self.cov_keys:
        k0=win0['corr_indxs']#cov_key

        win=win0#[i] #[k0]
        bin_wt=None

        corr=(k0[0],k0[1],k0[2],k0[3])
        s1s2s={}
        s1s2s[1324]=np.sort(np.array([self.cov_s1s2s(corr=(corr[0],corr[2])), #13
                                      self.cov_s1s2s(corr=(corr[1],corr[3])) #24
                                     ]))
        s1s2s[1324]=str(s1s2s[1324][0])+str(s1s2s[1324][1])
        s1s2s[1423]=np.sort(np.array([self.cov_s1s2s(corr=(corr[0],corr[3])), #14
                                    self.cov_s1s2s(corr=(corr[1],corr[2])) #23
                                    ]))
        s1s2s[1423]=str(s1s2s[1423][0])+str(s1s2s[1423][1])

        wig_3j_2_1324=wig_3j_2[s1s2s[1324]]
        wig_3j_2_1423=wig_3j_2[s1s2s[1423]]
        for corr_i in [1324,1423]:
            if corr_i==1423:
                wig_i=wig_3j_2_1423
                if self.bin_window:
                    bin_wt={'wt0':np.sqrt(win['bin_wt']['cl14']*win['bin_wt']['cl23'])} #FIXME: this is an approximation because we donot save unbinned covariance
                    bin_wt['wt_b']=np.sqrt(win['bin_wt']['cl_b14']*win['bin_wt']['cl_b23'])
                    if not np.all(bin_wt['wt_b']==0): #avoid NAN
                        bin_wt['wt_b']=1./bin_wt['wt_b']
            else:
                wig_i=wig_3j_2_1324
                if self.bin_window:
                    bin_wt={'wt0':np.sqrt(win['bin_wt']['cl13']*win['bin_wt']['cl24'])} #FIXME: this is an approximation because we donot save unbinned covariance
                    bin_wt['wt_b']=np.sqrt(win['bin_wt']['cl_b13']*win['bin_wt']['cl_b24'])
                    if not np.all(bin_wt['wt_b']==0):#avoid NAN
                        bin_wt['wt_b']=1./bin_wt['wt_b']
            for wp in win['W_pm'][corr_i]:
                win_t=self.coupling_matrix_large(win[corr_i], wig_3j_2=wig_i,mf_pm=mf_pm,W_pm=wp,
                                            bin_wt=bin_wt,lm=lm,cov=True)
                for k in win[corr_i].keys():
                    win['M'][corr_i][k][wp][lm]=win_t[k]
        i+=1
        return win

    def get_cov_coupling_all_lm(self,corr_indxs,win0,wig_3j_2,mf_pm):
        cov_lm={}
        for lm in self.lms:
            cov_lm[lm]=self.get_cov_coupling_lm(corr_indxs,win0,lm,wig_3j_2[lm],mf_pm[lm])
#             cov_lm[lm]=delayed(self.get_cov_coupling_lm)(win0,wig_3j_2[lm],mf_pm[lm])
        return cov_lm
    
    def combine_coupling_cov_xi(self,result):
        dic={}
        i=0
        for ii in self.cov_keys: #list(result.keys()):#np.arange(len(result)):
            result0=result[i]
            corr1=result0['corr1']
            corr2=result0['corr2']
            indx1=result0['indxs1']
            indx2=result0['indxs2']
            corr=corr1+corr2
            corr21=corr2+corr1
            indxs=indx1+indx2
            indxs2=indx2+indx1

            if dic.get(corr) is None:
                dic[corr]={}
            if dic.get(corr21) is None:
                dic[corr21]={}

            dic[corr][indxs]=result0

            dic[corr][indxs2]=result0
            dic[corr21][indxs2]=result0
            dic[corr21][indxs]=result0
            i+=1
        return dic
    
    def combine_coupling_cov(self,result):
        """
        This function combines the partial coupling matrices computed for covariance and returns a 
        dictionary of all coupling matrices.
        """
        dic={}
        nl=len(self.l)
        if self.bin_window:
            nl=len(self.l_bins)-1
        
        for ii_t in np.arange(len(self.cov_keys)): #list(result[0].keys()):#np.arange(len(result)):
            ii=0 #because we delete below
            result0={}

            for k in result[0][ii].keys():
                result0[k]=result[0][ii][k]

            W_pm=result[0][ii]['W_pm']
            corr1=result[0][ii]['corr1']
            corr2=result[0][ii]['corr2']
            indx1=result[0][ii]['indxs1']
            indx2=result[0][ii]['indxs2']

            result0['M']={1324:{},1423:{}}

            for corr_i in [1324,1423]:
                for k in result[0][ii]['M'][corr_i].keys():
                    result0['M'][corr_i][k]={}
                    for wp in W_pm[corr_i]:
                        result0['M'][corr_i][k][wp]=np.zeros((nl,nl))

            for i_lm in np.arange(len(self.lms)):
                lm=self.lms[i_lm]
                start_i=self.lms[i_lm]
                end_i=lm+self.step
                if self.bin_window:
                    start_i=0
                    end_i=nl
                for corr_i in [1324,1423]:
                    for wp in W_pm[corr_i]:
                        for k in result[lm][ii]['M'][corr_i].keys():
                            result0['M'][corr_i][k][wp][start_i:end_i,:]+=result[lm][ii]['M'][corr_i][k][wp][lm]

                del result[lm][ii]

            corr=corr1+corr2
            corr21=corr2+corr1
            indxs=indx1+indx2
            indxs2=indx2+indx1

            if dic.get(corr) is None:
                dic[corr]={}
            if dic.get(corr21) is None:
                dic[corr21]={}

            dic[corr][indxs]=result0

            dic[corr][indxs2]=result0
            dic[corr21][indxs2]=result0
            dic[corr21][indxs]=result0

        return dic

    def combine_single_coupling_cov(self,corr_indxs,result):
        """
        This function combines the partial coupling matrices computed for covariance and returns a 
        dictionary of all coupling matrices.
        """
        nl=len(self.l)
        if self.bin_window:
            nl=len(self.l_bins)-1
        
        ii=0 #because we delete below
        result0={}

        for k in result[0].keys():
            result0[k]=result[0][k]

        W_pm=result[0]['W_pm']
        corr1=result[0]['corr1']
        corr2=result[0]['corr2']
        indx1=result[0]['indxs1']
        indx2=result[0]['indxs2']

        result0['M']={1324:{},1423:{}}

        for corr_i in [1324,1423]:
            for k in result[0]['M'][corr_i].keys():
                result0['M'][corr_i][k]={}
                for wp in W_pm[corr_i]:
                    result0['M'][corr_i][k][wp]=np.zeros((nl,nl))


        #win['M'][1324][k][wp]

        for i_lm in np.arange(len(self.lms)):
            lm=self.lms[i_lm]
            start_i=self.lms[i_lm]
            end_i=lm+self.step
            if self.bin_window:
                start_i=0
                end_i=nl
            for corr_i in [1324,1423]:
                for wp in W_pm[corr_i]:
                    for k in result[lm]['M'][corr_i].keys():
                        result0['M'][corr_i][k][wp][start_i:end_i,:]+=result[lm]['M'][corr_i][k][wp][lm]

            del result[lm]

        return result0 #{corr_indxs:result0}

    
    def set_window_cl(self,corrs=None,corr_indxs=None,client=None,npartitions=100,use_bag=True):
        """
        This function sets the graph for computing power spectra of windows 
        for both C_ell and covariance matrices.
        """
        #if self.store_win and client is None:
         #   client=get_client()
        t1=time.time()
        self.cl_keys=[corr+indx for corr in corrs for indx in corr_indxs[corr]]
        self.cl_bag=dask.bag.from_sequence(self.cl_keys,npartitions=npartitions)
        
#         yy=pickle.dumps(self.get_window_power_cl) #this is the problem. copying this slows down map.
#         yy2=pickle.dumps(self.get_window_power_cov)
        print('cl bags doing',time.time()-t1, get_size_pickle(self.get_window_power_cov))#,get_size_pickle(self.z_bins))
        if use_bag:
            self.Win_cl=self.cl_bag.map(self.get_window_power_cl,c_ell0=self.c_ell0,c_ell_b=self.c_ell_b)#this can be slow... https://stackoverflow.com/questions/64559993/what-happens-during-dask-client-map-call
        else:
            self.Win_cl=[delayed(self.get_window_power_cl)(ck,c_ell0=self.c_ell0,c_ell_b=self.c_ell_b) for ck in self.cl_keys]
        print('cl bags done',self.Win_cl,time.time()-t1)
        self.cov_keys=[]
        self.Win_cov=None
        if self.do_cov:
            for corr in self.cov_indxs.keys():
                self.cov_keys+=[corr+indx for indx in self.cov_indxs[corr]]
                self.cov_bag=dask.bag.from_sequence(self.cov_keys,npartitions=npartitions)
                if use_bag:
                    self.Win_cov=self.cov_bag.map(self.get_window_power_cov,c_ell0=self.c_ell0,c_ell_b=self.c_ell_b)
                else:
                    self.Win_cov=[delayed(self.get_window_power_cov)(ck,c_ell0=self.c_ell0,c_ell_b=self.c_ell_b) for ck in self.cov_keys]
                # ^ is super slow for very large number of covariances. ^^ helps because dask effectively bunches up delayed.
                # ^ is better for smaller computes. ^^ for very large ones.
                # FIXME: We can probably implement custom partitions, bunching up covariances from same tracers to make things more efficient.
                # Custom partitions will likely require serialization of functions, so unlikely to help with dask map issue.
        print('cl+cov bags done',len(self.cl_keys),len(self.cov_keys),time.time()-t1)
        if self.store_win and client is not None:
           self.Win_cl=client.persist(self.Win_cl)
           if self.do_cov:
               self.Win_cov=client.persist(self.Win_cov)

    def combine_coupling_cl_cov(self,win_cl_lm,win_cov_lm):
        Win={}
        Win['cl']=self.combine_coupling_cl(win_cl_lm)
        if self.do_cov:
            Win['cov']=self.combine_coupling_cov(win_cov_lm)
        return Win
    
    def combine_coupling_xi_cov(self,win_cl,win_cov):
        Win={}
        Win['cl']=self.combine_coupling_xi(win_cl)
        if self.do_cov:
            Win['cov']=self.combine_coupling_cov_xi(win_cov)
        return Win

    def set_store_window(self,corrs=None,corr_indxs=None,client=None):
        """
        This function sets and computes the graph for the coupling matrices. It first calls the function to 
        generate graph for window power spectra, which is then combined with the graphs for wigner functions
        to get the final graph.
        """
        if self.store_win and client is None:
            client=get_client(address=self.scheduler_info['address']) #this seems to be the correct thing to do
        print('setting windows, coupling matrices ',client,client.scheduler_info())
        
        self.set_window_cl(corrs=corrs,corr_indxs=corr_indxs,client=client)
        print('got window cls, now to coupling matrices.',len(self.cl_keys),len(self.cov_keys),self.Win_cl )
        
        self.Win={'cl':{}}
        Win_cl=self.Win_cl
        Win_cov=self.Win_cov
        if self.do_pseudo_cl: #this is probably redundant due to if statement in init.
            self.Win_cl_lm={}
            self.Win_cov_lm={}
            self.Win_lm={}

            for lm in self.lms:
                print('doing lm',lm)
                t1=time.time()
                self.Win_cl_lm[lm]={}
                self.Win_lm[lm]={}

                self.Win_cl_lm[lm]=self.cl_bag.map(self.get_cl_coupling_lm,Win_cl,lm,
                                               self.wig_3j_2[lm],self.mf_pm[lm])

#                 self.Win_cl_lm[lm]=[delayed(self.get_cl_coupling_lm)(Wc,lm,
#                                                                      self.wig_3j_2[lm],self.mf_pm[lm]) for Wc in Win_cl]

                self.Win_lm[lm]['cl']=self.Win_cl_lm[lm]
                print('done lm cl graph',lm,time.time()-t1)
                if self.do_cov:
                    self.Win_cov_lm[lm]=self.cov_bag.map(self.get_cov_coupling_lm,Win_cov,lm,
                                                   self.wig_3j_2[lm],self.mf_pm[lm])

#                     self.Win_cov_lm[lm]=[delayed(self.get_cov_coupling_lm)(Wc,lm,
#                                                                  self.wig_3j_2[lm],self.mf_pm[lm]) for Wc in Win_cov]

                    self.Win_lm[lm]['cov']=self.Win_cov_lm[lm]
                print('done lm cl+cov graph',lm,time.time()-t1,get_size_pickle(self.get_cl_coupling_lm))
#### Donot delete
                if self.store_win:  
                   self.Win_lm[lm]=client.compute(self.Win_lm[lm]).result() #use client persist

                   self.Win_cl_lm[lm]=self.Win_lm[lm]['cl']#.result()

                   if self.do_cov:
                       self.Win_cov_lm[lm]=self.Win_lm[lm]['cov']

                   del self.wig_3j_2[lm]
                   del self.mf_pm[lm]
                   gc.collect()
                print('done lm',lm,time.time()-t1)
                    
            self.Win=delayed(self.combine_coupling_cl_cov)(self.Win_cl_lm,self.Win_cov_lm)
            self.Win_cl=delayed(self.combine_coupling_cl)(self.Win_cl_lm)
            
            if self.do_cov:
                self.Win_cov=delayed(self.combine_coupling_cov)(self.Win_cov_lm)
            print('done combine lm graph',time.time()-t1)

        if self.store_win:
            self.Win=client.compute(self.Win).result()
            
            if self.do_pseudo_cl:
                self.cleanup()
        return self.Win
    
    def reduce_win_cl(self,win,win2):
        print(win,win2)
        dic=win
        corr=win2['corr']
        corr2=corr[::-1]
        indxs=win2['indxs']
        indxs2=indxs[::-1]
        if dic.get(corr) is None:
            self.Win['cl'][corr]={}
        if dic.get(corr2) is None:
            self.Win['cl'][corr2]={}
        dic[corr2][indxs2]=win2
        dic[corr][indxs]=win2
        return dic
    
    def set_window_graph(self,corrs=None,corr_indxs=None,client=None,npartitions=2):
        """
        This function sets the graph for the coupling matrices. 
        This graph is different from the one used in set_store_window.
        It first calls the function to 
        generate graph for window power spectra, which is then combined with the graphs for wigner functions
        to get the final graph.
        """
        print('setting windows graph only, coupling matrices ',client)
        
        self.set_window_cl(corrs=corrs,corr_indxs=corr_indxs,client=client,npartitions=npartitions,use_bag=False)
        print('got window cls, now to coupling matrices.',len(self.cl_keys),len(self.cov_keys))#,self.Win_cl )
        
        self.Win={'cl':{}}
        Win_cl=self.Win_cl#.to_delayed()
        Win_cov=self.Win_cov#.to_delayed()
        if self.do_pseudo_cl: #this is probably redundant due to if statement in init.
            self.Win_cl_lm={}
            self.Win_cov_lm={}
            self.Win_lm={}
#             self.Win_cl_lm=dask.bag.from_sequence(self.cl_keys,npartitions=npartitions).map(self.get_cl_coupling_all_lm,Win_cl,
#                                                             self.wig_3j_2,self.mf_pm)#.to_delayed()
#             self.WinM_cl=dask.bag.from_sequence(self.cl_keys,npartitions=npartitions).map(self.combine_single_coupling_cl,self.Win_cl_lm)#.to_delayed()
            ##             self.WinM_cl=self.WinM_cl.take(len(self.cl_keys),-1,compute=False).to_delayed()
            self.Win_cl_lm=[{lm:delayed(self.get_cl_coupling_lm)(None,Wc,lm,
                                                            self.wig_3j_2[lm],self.mf_pm[lm]) for lm in self.lms} for Wc in self.Win_cl]
            self.WinM_cl=[delayed(self.combine_single_coupling_cl)(None,Wc) for Wc in self.Win_cl_lm]
            
            print('done cl graph')
            if self.do_cov:
#                 self.Win_cov_lm=dask.bag.from_sequence(self.cov_keys,npartitions=npartitions).map(self.get_cov_coupling_all_lm,Win_cov,
#                                                             self.wig_3j_2,self.mf_pm)#.to_delayed()
#                 self.WinM_cov=dask.bag.from_sequence(self.cov_keys,npartitions=npartitions).map(self.combine_single_coupling_cov,self.Win_cov_lm)#.to_delayed()
#         #                 self.WinM_cov=self.WinM_cov.take(len(self.cov_keys),-1,compute=False).to_delayed()
                self.Win_cov_lm=[{lm: delayed(self.get_cov_coupling_lm)(None,Wc,lm,self.wig_3j_2[lm],self.mf_pm[lm]) for lm in self.lms} for Wc in self.Win_cov]
                self.WinM_cov=[delayed(self.combine_single_coupling_cov)(None,Wc) for Wc in self.Win_cov_lm]


            print('done cl+cov graph',self.WinM_cl)#,len(self.WinM_cl))
            
            self.Win={'cl':{},'cov':{}}
            i=0
            for k in self.cl_keys:
                corr=(k[0],k[1])
                corr2=corr[::-1]
                indxs=(k[2],k[3])
                indxs2=indxs[::-1]
                if self.Win['cl'].get(corr) is None:
                    self.Win['cl'][corr]={}
                if self.Win['cl'].get(corr2) is None:
                    self.Win['cl'][corr2]={}
                self.Win['cl'][corr2][indxs2]=self.WinM_cl[i]#.pluck(k)
                self.Win['cl'][corr][indxs]=self.WinM_cl[i]#self.Win['cl'][corr2][indxs2]
                i+=1
                
            i=0
            for k in self.cov_keys:
                corr1=(k[0],k[1])
                corr2=(k[2],k[3])
                indxs1=(k[4],k[5])
                indxs2=(k[6],k[7])
                corr=corr1+corr2
                indxs=indxs1+indxs2
                if self.Win['cov'].get(corr) is None:
                    self.Win['cov'][corr]={}
                self.Win['cov'][corr][indxs]=self.WinM_cov[i]#.pluck(k)
                i+=1
                    

        if self.store_win:
            self.Win=client.compute(self.Win).result()
            
            if self.do_pseudo_cl:
                self.cleanup()
        return self.Win

    def cleanup(self,): #need to free all references to wigner_3j, mf and wigner_3j_2... this doesnot help with peak memory usage
        del self.Win_cl
        del self.Win_cl_lm
        if self.do_cov:
            del self.Win_cov
            del self.Win_cov_lm
        del self.Win_lm
        del self.wig_3j
        del self.wig_3j_2
        del self.mf_pm
