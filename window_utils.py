import dask
from dask import delayed
import sparse
from wigner_transform import *
from binning import *
from cov_utils import *
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

class window_utils():
    def __init__(self,window_l=None,window_lmax=None,l=None,corrs=None,m1_m2s=None,use_window=None,f_sky=None,
                do_cov=False,cov_utils=None,corr_indxs=None,z_bins=None,HT=None,xi_bin_utils=None,do_xi=False,
                store_win=False,Win=None,wigner_files=None,step=None,xi_win_approx=False):
        self.Win=Win
        self.wigner_files=wigner_files
        self.wig_3j=None
        self.window_lmax=window_lmax
        self.window_l=window_l
        self.l=l
        self.HT=HT #for correlation windows
        self.corrs=corrs
        self.m1_m2s=m1_m2s
        self.use_window=use_window
        self.do_cov=do_cov
        self.do_xi=do_xi
        self.xi_bin_utils=xi_bin_utils
        self.binning=binning()
        self.cov_utils=cov_utils
        self.corr_indxs=corr_indxs
        self.z_bins=z_bins
        self.f_sky=f_sky
        self.store_win=store_win

        nl=len(self.l)
        nwl=len(self.window_l)*1.0

        self.step=step
        if step is None:
            self.step=np.int32(100.*((2500./nl)**2)*(1000./nwl)) #small step is useful for lower memory load
            self.step=min(self.step,nl+1)
        self.lms=np.arange(nl,step=self.step)
        print('Win gen: step size',self.step)

        self.Win=Win
        if self.Win is None and self.use_window:
            if self.do_xi:
                print('Warning: window for xi is different from cl. Only one of xi or cl is supported. Hence cl window will be wrong.')
            self.set_wig3j()
            self.set_window(corrs=self.corrs,corr_indxs=self.corr_indxs)
        elif self.do_xi and xi_win_approx:
            self.Win={'cl':{corr:{} for corr in self.corrs},'cov':{corr1+corr2: {} for corr1 in self.corrs for corr2 in self.corrs}}
            self.set_window_cl(corrs=self.corrs,corr_indxs=self.corr_indxs)

            for k in self.Win_cl.keys():
                wint=self.Win_cl[k]
                self.Win['cl'][wint['corr']][wint['indxs']]=wint
            if self.do_cov:
                for k in self.Win_cov.keys():
                    wint=self.Win_cov[k]
                    corrs=wint['corr1']+wint['corr2']
                    indxs=wint['indxs1']+wint['indxs2']
                    self.Win['cov'][corrs][indxs]=wint
#                     self.Win['cov']=self.Win_cov


    def wig3j_step_read(self,m=0,lm=None):
        step=self.step
        out=self.wig_3j[m].oindex[np.int32(self.window_l),np.int32(self.l[lm:lm+step]),np.int32(self.l)]
        out=out.transpose(1,2,0)
        return out

    def set_wig3j_step_multiplied(self,wig1,wig2):
#         out=sparse.COO(wig1*wig2.astype('float64')) #sparse leads to small hit in in time when doing dot products but helps with the memory overall.
        out=wig1*wig2.astype('float64') #numpy dot appears to run faster with 64bit ... ????
        return out

    def set_wig3j_step_spin(self,wig2,mf_pm,W_pm):
        if W_pm==2: #W_+
            mf=mf_pm['mf_p']#.astype('float64') #https://stackoverflow.com/questions/45479363/numpy-multiplying-large-arrays-with-dtype-int8-is-slow
        if W_pm==-2: #W_+
            mf=1-mf_pm['mf_p']
        return wig2*mf

    def set_window_pm_step(self,lm=None):
        li1=np.int32(self.window_l).reshape(len(self.window_l),1,1)
        li3=np.int32(self.l).reshape(1,1,len(self.l))
        li2=np.int32(self.l[lm:lm+self.step]).reshape(1,len(self.l[lm:lm+self.step]),1)
        mf=(-1)**(li1+li2+li3)
        mf=mf.transpose(1,2,0)
        out={}
#         out['mf_p']=(1.+mf)/2.
        out['mf_p']=np.int8((1.+mf)/2.)#.astype('bool') #memory hog...
                              #bool doesn't help in itself, as it is also byte size in numpy.
                                #we donot need to store mf_n, as it is simply a 0-1 flip or "not" when written as bool
                                #using bool or int does cost somewhat in computation as numpy only computes with float 64 (or 32 in 32 bit systems). If memory is not an
                            #issue, use float64 here and then use mf_n=1-mf_p.
#         del mf
        return out

    def set_wig3j(self):
        self.wig_3j={}
        if not self.use_window:
            return

        m_s=np.concatenate([np.abs(i).flatten() for i in self.m1_m2s.values()])
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
        client=get_client()
        for lm in self.lms:
            self.wig_3j_2[lm]={}
            self.wig_3j_1[lm]={m1: delayed(self.wig3j_step_read)(m=m1,lm=lm) for m1 in self.m_s}
            self.mf_pm[lm]=delayed(self.set_window_pm_step)(lm=lm)
            mi=0
            for m1 in self.m_s:
                for m2 in self.m_s[mi:]:
                    self.wig_3j_2[lm][str(m1)+str(m2)]={}
                    self.wig_3j_2[lm][str(m1)+str(m2)][0]=delayed(self.set_wig3j_step_multiplied)(self.wig_3j_1[lm][m1],self.wig_3j_1[lm][m2])
#                     if m1>0 or m2>0:
#                     self.wig_3j_2[lm][str(m1)+str(m2)][2]=delayed(self.set_wig3j_step_spin)(self.wig_3j_2[lm][str(m1)+str(m2)][0],self.mf_pm[lm],2)
#                                                                                         #FIXME: for covariance, we sometimes use 0, but pm factor is still there
# #                     if m1>0 and m2>0:
#                     self.wig_3j_2[lm][str(m1)+str(m2)][-2]=delayed(self.set_wig3j_step_spin)(self.wig_3j_2[lm][str(m1)+str(m2)][0],self.mf_pm[lm],-2)
                mi+=1


        self.wig_m1m2s={}
        for corr in self.corrs:
            mi=np.sort(np.absolute(self.m1_m2s[corr]).flatten())
            self.wig_m1m2s[corr]=str(mi[0])+str(mi[1])
        print('wigner done',self.wig_3j.keys())


    def coupling_matrix(self,win,wig_3j_1,wig_3j_2,W_pm=0):
        return np.dot(wig_3j_1*wig_3j_2,win*(2*self.window_l+1)   )/4./np.pi

#     def coupling_matrix_large(self,win,m1m2,wig_3j_2,mf_pm,lm=None,W_pm=0):
    def coupling_matrix_large(self,win,wig_3j_2,mf_pm,W_pm=0):
        wig=wig_3j_2[0] #[W_pm]

        if W_pm!=0:
            if W_pm==2: #W_+
                wig=wig*mf_pm['mf_p']#.astype('float64') #https://stackoverflow.com/questions/45479363/numpy-multiplying-large-arrays-with-dtype-int8-is-slow
            if W_pm==-2: #W_-
                wig=wig*(1-mf_pm['mf_p'])
#             wig=wig*mf #this can still blow up peak memory
                                                                            #M[lm:lm+step,:]
                            #         M=np.einsum('ijk,i->jk',wig.todense()*mf, win*(2*self.window_l+1), optimize=True )/4./np.pi
        M={}
        for k in win.keys():
            M[k]=wig@(win[k]*(2*self.window_l+1))
            M[k]/=4.*np.pi
        if W_pm!=0:
            del wig
        return M

    def multiply_window(self,win1,win2):
        W=win1*win2
        x=np.logical_or(win1==hp.UNSEEN, win2==hp.UNSEEN)
        W[x]=hp.UNSEEN
        return W

    def mask_comb(self,win1,win2): #for covariance, specially SSC
        W=win1*win2
        W/=W
        x=np.logical_or(win1==hp.UNSEEN, win2==hp.UNSEEN)
        W[x]=hp.UNSEEN
        fsky=(~x).mean()
        return fsky,W.astype('int16')

    def get_window_power_cl(self,corr={},indxs={}):
#         print('cl window doing',corr,indxs)
        win={}
        win['corr']=corr
        win['indxs']=indxs
#         if not self.use_window:
#             win={'cl':self.f_sky, 'M':self.coupling_M,'xi':1,'xi_b':1}
#             return win

        m1m2=np.absolute(self.m1_m2s[corr]).flatten()
        W_pm=0
        if np.sum(m1m2)!=0:
            W_pm=2 #we only deal with E mode\
            if corr==('shearB','shearB'):
                W_pm=-2
        if self.do_xi:
            W_pm=0 #for xi estimators, there is no +/-. Note that this will result in wrong thing for pseudo-C_ell.
                    #FIXME: hence pseudo-C_ell and xi together are not supported right now

        z_bin1=self.z_bins[corr[0]][indxs[0]]
        z_bin2=self.z_bins[corr[1]][indxs[1]]
        alm1=z_bin1['window_alm']
        alm2=z_bin2['window_alm']

        win[12]={} #to keep some naming uniformity with the covariance window
        win[12]['cl']=hp.alm2cl(alms1=alm1,alms2=alm2,lmax_out=self.window_lmax) #This is f_sky*cl.

        alm1=z_bin1['window_alm_noise']
        alm2=z_bin2['window_alm_noise']

        if corr[0]==corr[1] and indxs[0]==indxs[1]:
            win[12]['N']=hp.alm2cl(alms1=alm1,alms2=alm2,lmax_out=self.window_lmax) #This is f_sky*cl.


        win['W_pm']=W_pm
        win['m1m2']=m1m2
        if self.do_xi:
            th,win['xi']=self.HT.projected_correlation(l_cl=self.window_l,m1_m2=(0,0),cl=win[12]['cl'])
            win['xi_b']=self.binning.bin_1d(xi=win['xi'],bin_utils=self.xi_bin_utils[(0,0)])

        win['M']={} #self.coupling_matrix_large(win['cl'], m1m2,wig_3j_2=wig_3j_2,W_pm=W_pm)*(2*self.l[:,None]+1) #FIXME: check ordering
        win['M_noise']=None
        return win

    def get_cl_coupling_lm(self,win,lm,wig_3j_2,mf_pm):
        win2={'M':{},'M_noise':None,'M_B':None,'M_B_noise':None}
        if lm==0:
            win2=win
        win_M=self.coupling_matrix_large(win[12], wig_3j_2=wig_3j_2,mf_pm=mf_pm,W_pm=win['W_pm'])
#         win_M=self.coupling_matrix_large(win[12],wig_3j_2=wig_3j_2[win['W_pm']])
        win2['M'][lm]=win_M['cl']
        if 'N' in win_M.keys():
            win2['M_noise']={lm:win_M['N']}
        if win['corr']==('shear','shear') and win['indxs'][0]==win['indxs'][1]: #FIXME: this should be dprecated once shearB is implemented.

            if not self.do_xi:#FIXME: hence pseudo-C_ell and xi together are not supported right now. for xi, window is same in xi+/-
                win_M=self.coupling_matrix_large(win[12],wig_3j_2=wig_3j_2,mf_pm=mf_pm,W_pm=-2)
#             win_M=self.coupling_matrix_large(win[12],wig_3j_2=wig_3j_2[-2])
            win2['M_B_noise']={}
            win2['M_B']={}
            win2['M_B_noise'][lm]=win_M['N']
            win2['M_B'][lm]=win_M['cl']
        return win2

    def return_dict_cl(self,result,corrs): #combine partial matrices
        dic={}
        nl=len(self.l)

        for corr in corrs:
            dic[corr]={}
            dic[corr[::-1]]={}

        for ii in list(result[0].keys()):

            result_ii=result[0][ii]
            corr=result_ii['corr']
            indxs=result_ii['indxs']

            result0={}
            for k in result_ii.keys():
                result0[k]=result_ii[k]

            result0['M']=np.zeros((nl,nl))
            if  result_ii['M_noise'] is not None:
                result0['M_noise']=np.zeros((nl,nl))
            if corr==('shear','shear') and indxs[0]==indxs[1]:#FIXME: this should be dprecated once shearB is implemented.
                result0['M_B_noise']=np.zeros((nl,nl))
                result0['M_B']=np.zeros((nl,nl))

            for lm in self.lms:
                result0['M'][lm:lm+self.step,:]+=result[lm][ii]['M'][lm]
                if  result_ii['M_noise'] is not None:
                    result0['M_noise'][lm:lm+self.step,:]+=result[lm][ii]['M_noise'][lm]
                if corr==('shear','shear') and indxs[0]==indxs[1]:#FIXME: this should be dprecated once shearB is implemented.
                    result0['M_B_noise'][lm:lm+self.step,:]+=result[lm][ii]['M_B_noise'][lm]
                    result0['M_B'][lm:lm+self.step,:]+=result[lm][ii]['M_B'][lm]

                del result[lm][ii]
            result0['M']*=(2*self.l[:,None]+1)
            if  result_ii['M_noise'] is not None:
                result0['M_noise']*=(2*self.l[:,None]+1)
            if corr==('shear','shear') and indxs[0]==indxs[1]:#FIXME: this should be dprecated once shearB is implemented.
                result0['M_B_noise']*=(2*self.l[:,None]+1)
                result0['M_B']*=(2*self.l[:,None]+1)
            dic[corr][indxs]=result0
            dic[corr[::-1]][indxs[::-1]]=result0

        return dic

    def cov_m1m2s(self,corr): #when spins are not same, we set them to 0. Expressions are not well defined in this case. Should be ok for l>~50 ish
            m1m2=np.absolute(self.m1_m2s[corr]).flatten()
            if m1m2[0]==m1m2[1]:
                return m1m2[0]
            else:
                return 0

    def get_window_power_cov(self,corr1=None,corr2=None,indxs1=None,indxs2=None):
        win={}
        corr=corr1+corr2
        indxs=indxs1+indxs2
        win['corr1']=corr1
        win['corr2']=corr2
        win['indxs1']=indxs1
        win['indxs2']=indxs2
#         if not self.use_window:
#             win={'cl1324':self.f_sky,'M1324':self.coupling_G, 'M1423':self.coupling_G, 'cl1423':self.f_sky}
#             return win

        def get_window_spins(cov_indxs=[(0,2),(1,3)]):    #W +/- factors based on spin
            W_pm=[0]
            if self.do_xi:
                return W_pm#for xi estimators, there is no +/-. Note that this will result in wrong thing for pseudo-C_ell.
                    #FIXME: hence pseudo-C_ell and xi together are not supported right now

            s=[np.sum(self.m1_m2s[corr1]),np.sum(self.m1_m2s[corr2])]

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


        m1m2s={}

        m1m2s[1324]=np.array([self.cov_m1m2s(corr=(corr[0],corr[2])), #13
                              self.cov_m1m2s(corr=(corr[1],corr[3])) #24
                    ])

        m1m2s[1423]=np.array([self.cov_m1m2s(corr=(corr[0],corr[3])), #14
                              self.cov_m1m2s(corr=(corr[1],corr[2])) #23
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
                        )

        if corr[0]==corr[2] and indxs[0]==indxs[2]: #noise X cl
            win[1324]['Ncl']=hp.anafast(map1=z_bin1['window'],
                                 map2=self.multiply_window(z_bin2['window'],z_bin4['window']),
                                 lmax=self.window_lmax
                        )
        if corr[1]==corr[3] and indxs[1]==indxs[3]:#noise X cl
            win[1324]['clN']=hp.anafast(map1=self.multiply_window(z_bin1['window'],z_bin3['window']),
                                 map2=z_bin2['window'],
                                 lmax=self.window_lmax
                        )
        if corr[0]==corr[2] and indxs[0]==indxs[2] and corr[1]==corr[3] and indxs[1]==indxs[3]: #noise X noise
            win[1324]['NN']=hp.anafast(map1=z_bin1['window'],
                                 map2=z_bin2['window'],
                                 lmax=self.window_lmax
                        )

        win[1423]['clcl']=hp.anafast(map1=self.multiply_window(z_bin1['window'],z_bin4['window']),
                                 map2=self.multiply_window(z_bin2['window'],z_bin3['window']),
                                 lmax=self.window_lmax
                            )

        if corr[0]==corr[3] and indxs[0]==indxs[3]: #noise14 X cl
            win[1423]['Ncl']=hp.anafast(map1=z_bin1['window'],
                                 map2=self.multiply_window(z_bin2['window'],z_bin3['window']),
                                 lmax=self.window_lmax
                        )
        if corr[1]==corr[2] and indxs[1]==indxs[2]:#noise23 X cl
            win[1423]['clN']=hp.anafast(map1=self.multiply_window(z_bin1['window'],z_bin4['window']),
                                 map2=z_bin2['window'],
                                 lmax=self.window_lmax
                        )
        if corr[0]==corr[3] and indxs[0]==indxs[3] and corr[1]==corr[2] and indxs[1]==indxs[2]: #noise X noise
            win[1423]['NN']=hp.anafast(map1=z_bin1['window'],
                                 map2=z_bin2['window'],
                                 lmax=self.window_lmax
                        )

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

        win['M']={1324:{},1423:{}}

        for k in win[1324].keys():
            win['M'][1324][k]={wp:{} for wp in W_pm[1324]}
        for k in win[1423].keys():
            win['M'][1423][k]={wp:{} for wp in W_pm[1423]}

        win['xi']={1324:{},1423:{}}
        win['xi_b']={1324:{},1423:{}}
        if self.do_xi:
            for k in win[1324].keys():
                th,win['xi'][1324][k]=self.HT.projected_covariance(l_cl=self.window_l,m1_m2=(0,0),cl_cov=win[1324][k])
                win['xi_b'][1324][k]=self.binning.bin_2d(cov=win['xi'][1324][k],bin_utils=self.xi_bin_utils[(0,0)])
            for k in win[1423].keys():
                th,win['xi'][1423][k]=self.HT.projected_covariance(l_cl=self.window_l,m1_m2=(0,0),cl_cov=win[1423][k])
                win['xi_b'][1423][k]=self.binning.bin_2d(cov=win['xi'][1423][k],bin_utils=self.xi_bin_utils[(0,0)])

        win['W_pm']=W_pm
        win['m1m2']=m1m2s
        return win

    def get_cov_coupling_lm(self,win,lm,wig_3j_2_1324,wig_3j_2_1423,mf_pm,m1m2s):
        for corr_i in [1324,1423]:
            wig_i=wig_3j_2_1324
            if corr_i==1423:
                wig_i=wig_3j_2_1423
            for wp in win['W_pm'][corr_i]:
                win_t=self.coupling_matrix_large(win[corr_i], wig_3j_2=wig_i,mf_pm=mf_pm,W_pm=wp)
#                 win_t=self.coupling_matrix_large(win[corr_i],wig_i[wp])
                for k in win[corr_i].keys():
                    win['M'][corr_i][k][wp][lm]=win_t[k]
#                     win['M'][corr_i][k][wp][lm]=self.coupling_matrix_large(win[corr_i][k], win['m1m2'][corr_i],lm=lm,wig_3j_2=wig_3j_2_1324,mf_pm=mf_pm,W_pm=wp)
        return win

    def return_dict_cov(self,result,win_cov_tuple): #to compute the covariance graph generated in set window
        dic={}
        nl=len(self.l)

        for ii in list(result[0].keys()):#np.arange(len(result)):
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


            #win['M'][1324][k][wp]

            for lm in self.lms:
                for corr_i in [1324,1423]:
                    for wp in W_pm[corr_i]:
                        for k in result[lm][ii]['M'][corr_i].keys():
                            result0['M'][corr_i][k][wp][lm:lm+self.step,:]+=result[lm][ii]['M'][corr_i][k][wp][lm]

                del result[lm][ii]

#             for wp in W_pm[1324]:
#                 result0['M1324'][wp]=sparse.COO(result0['M1324'][wp]) #covariance coupling matrices are stored as sparse.
#                                                         #because there are more of them and are only needed occasionaly.
#             for wp in W_pm[1423]:
#                 result0['M1423'][wp]=sparse.COO(result0['M1423'][wp])

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

    def set_window_cl(self,corrs=None,corr_indxs=None,client=None):
        if self.store_win and client is None:
            client=get_client()

        print('setting windows',client)

        self.Win_cl={corr+indx: delayed(self.get_window_power_cl)(corr,indx) for corr in corrs for indx in corr_indxs[corr]}
#         self.Win_cl={corr+indx: self.get_window_power_cl(corr,indx) for corr in corrs for indx in corr_indxs[corr]}

        if self.do_cov:
            self.Win_cov={}
            self.win_cov_tuple=None
            for ic1 in np.arange(len(corrs)):
                corr1=corrs[ic1]
                indxs_1=corr_indxs[corr1]
                n_indx1=len(indxs_1)

                for ic2 in np.arange(ic1,len(corrs)):
                    corr2=corrs[ic2]
                    indxs_2=corr_indxs[corr2]
                    n_indx2=len(indxs_2)

                    corr=corr1+corr2

                    for i1 in np.arange(n_indx1):
                        start2=0
                        indx1=indxs_1[i1]
                        if corr1==corr2:
                            start2=i1
                        for i2 in np.arange(start2,n_indx2):
                            indx2=indxs_2[i2]
                            indxs=indx1+indx2

                            self.Win_cov.update({corr+indxs: delayed(self.get_window_power_cov)(corr1,corr2,indx1,indx2)})

                            if self.win_cov_tuple is None:
                                self.win_cov_tuple=[(corr1,corr2,indx1,indx2)]
                            else:
                                self.win_cov_tuple.append((corr1,corr2,indx1,indx2))

        if self.store_win:
            self.Win_cl=client.compute(self.Win_cl)
            if self.do_cov:
                self.Win_cov=client.compute(self.Win_cov)
                self.Win_cov=self.Win_cov.result()
            self.Win_cl=self.Win_cl.result()




    def set_window(self,corrs=None,corr_indxs=None,client=None):
        self.set_window_cl(corrs=corrs,corr_indxs=corr_indxs,client=client)
        if self.store_win and client is None:
            client=get_client()
        print('got window cls, now to coupling matrices.')
        self.Win={'cl':{}}

        self.Win_cl_lm={}
        self.Win_cov_lm={}

        for lm in self.lms:
            t1=time.time()
            self.Win_cl_lm[lm]={}
            for k in self.Win_cl.keys():
                corr=(k[0],k[1])
                self.Win_cl_lm[lm][k]=delayed(self.get_cl_coupling_lm)(self.Win_cl[k],lm,self.wig_3j_2[lm][self.wig_m1m2s[corr]],self.mf_pm[lm])
            if self.store_win:
                self.Win_cl_lm[lm]=client.compute(self.Win_cl_lm[lm])#.result()

            if self.do_cov:
#             for lm in self.lms:
                self.Win_cov_lm[lm]={}
                for k in self.Win_cov.keys():
                    corr=(k[0],k[1],k[2],k[3])
                    m1m2s={}
                    m1m2s[1324]=np.sort(np.array([self.cov_m1m2s(corr=(corr[0],corr[2])), #13
                                          self.cov_m1m2s(corr=(corr[1],corr[3])) #24
                                        ]))
                    m1m2s[1324]=str(m1m2s[1324][0])+str(m1m2s[1324][1])
                    m1m2s[1423]=np.sort(np.array([self.cov_m1m2s(corr=(corr[0],corr[3])), #14
                                          self.cov_m1m2s(corr=(corr[1],corr[2])) #23
                                        ]))
                    m1m2s[1423]=str(m1m2s[1423][0])+str(m1m2s[1423][1])

                    self.Win_cov_lm[lm][k]=delayed(self.get_cov_coupling_lm)(self.Win_cov[k],lm,self.wig_3j_2[lm][m1m2s[1324]],self.wig_3j_2[lm][m1m2s[1423]],self.mf_pm[lm],m1m2s )

                if self.store_win:
                    self.Win_cov_lm[lm]=client.compute(self.Win_cov_lm[lm])#.result()
            t3=time.time()
            if self.store_win:
                self.Win_cl_lm[lm]=self.Win_cl_lm[lm].result()
                t4=time.time()
                if self.do_cov:
                    self.Win_cov_lm[lm]=self.Win_cov_lm[lm].result()
                t2=time.time()
                print('done coupling submatrix ',lm, t2-t1,t3-t1,t4-t3)
                del self.wig_3j_2[lm]
                del self.mf_pm[lm]
                gc.collect()
                t3=time.time()

        self.Win_cl=delayed(self.return_dict_cl)(self.Win_cl_lm,corrs)
        if self.store_win:
            self.Win['cl']=client.compute(self.Win_cl)#.result()
        else:
            self.Win['cl']=self.Win_cl

        if self.do_cov:
            self.Win_cov=delayed(self.return_dict_cov)(self.Win_cov_lm,self.win_cov_tuple)
            if self.store_win:
#                 self.Win['cov']=self.Win_cov.compute() #apparently client.compute has better memeory manangement than simple compute https://distributed.dask.org/en/latest/memory.html
                self.Win['cov']=client.compute(self.Win_cov)#.result()
            else:
                self.Win['cov']=self.Win_cov

        if self.store_win:
            if self.do_cov:
                self.Win['cov']=self.Win['cov'].result()
            self.Win['cl']=self.Win['cl'].result()
            self.cleanup()
        return self.Win

    def cleanup(self,): #need to free all references to wigner_3j, mf and wigner_3j_2... this doesnot help with peak memory usage
        del self.Win_cl
        del self.Win_cl_lm
        if self.do_cov:
            del self.Win_cov
        del self.Win_cov_lm
        del self.wig_3j
        del self.wig_3j_2
        del self.mf_pm
