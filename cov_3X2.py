import os,sys
import dask
from dask import delayed
from power_spectra import *
from angular_power_spectra import *
from hankel_transform import *
from wigner_transform import *
from binning import *
from cov_utils import *
from tracer_utils import *
from window_utils import *
from cov_tri import *
from astropy.constants import c,G
from astropy import units as u
import numpy as np
from scipy.interpolate import interp1d
import warnings,logging

d2r=np.pi/180.
c=c.to(u.km/u.second)

#corrs=['gg','gl_p','gl_k','ll_p','ll_m','ll_k','ll_kp']

class cov_3X2():
    def __init__(self,silence_camb=False,l=np.arange(2,2001),HT=None,Ang_PS=None,
                cov_utils=None,logger=None,tracer_utils=None,#lensing_utils=None,galaxy_utils=None,
                zs_bins=None,zk_bins=None,zg_bins=None,galaxy_bias_func=None,
                power_spectra_kwargs={},HT_kwargs=None,
                z_PS=None,nz_PS=100,log_z_PS=True,
                do_cov=False,SSV_cov=False,tidal_SSV_cov=False,do_sample_variance=True,
                Tri_cov=False,
                use_window=True,window_lmax=None,store_win=False,Win=None,
                f_sky=None,l_bins=None,bin_cl=False,#pseudo_cl=False,
                stack_data=False,bin_xi=False,do_xi=False,theta_bins=None,
                xi_win=False,
                corrs=[('shear','shear')],corr_indxs={},
                 wigner_files=None):
        
        self.logger=logger
        if logger is None:
            self.logger=logging.getLogger() #not really being used right now
            self.logger.setLevel(level=logging.DEBUG)
            logging.basicConfig(format='%(asctime)s %(levelname)s:%(message)s',
                                level=logging.DEBUG, datefmt='%I:%M:%S')

        self.do_cov=do_cov
        self.SSV_cov=SSV_cov
        self.Tri_cov=Tri_cov #small scale trispectrum
        self.tidal_SSV_cov=tidal_SSV_cov
        self.l=l
        self.do_xi=do_xi
        self.xi_win=xi_win
        self.corrs=corrs

        self.window_lmax=30 if window_lmax is None else window_lmax
        self.window_l=np.arange(self.window_lmax+1)
        self.f_sky=f_sky #should be a dict with full overlap entries for all tracers and bins.
                        #If scalar will be converted to dict later in this function

        if zs_bins is not None:
            z_PS_max=zs_bins['zmax']
        if zk_bins is not None:
            z_PS_max=zk_bins['zmax']
        if zk_bins is not None and zs_bins is not None:
            z_PS_max=max(z_PS_max,zk_bins['zmax'])
        if zk_bins is None and zs_bins is None: #z_PS_max is to defined maximum z for which P(k) is computed. 
                                                #We assume this will be larger for shear than galaxies (i.e. there are always sources behind galaxies).
            z_PS_max=zg_bins['zmax']
            
        self.use_window=use_window

        self.HT=None
        if do_xi:
            self.set_HT(HT=HT,HT_kwargs=HT_kwargs)

        self.tracer_utils=tracer_utils
        if tracer_utils is None:
            self.tracer_utils=Tracer_utils(zs_bins=zs_bins,zg_bins=zg_bins,zk_bins=zk_bins,
                                            logger=self.logger,l=self.l)

        self.cov_utils=cov_utils
        if cov_utils is None:
            self.cov_utils=Covariance_utils(f_sky=f_sky,l=self.l,logger=self.logger,
                                            do_xi=do_xi,
                                            do_sample_variance=do_sample_variance,
                                            use_window=use_window,
                                            window_l=self.window_l)

        self.Ang_PS=Ang_PS
        if Ang_PS is None:
            self.Ang_PS=Angular_power_spectra(silence_camb=silence_camb,
                                SSV_cov=SSV_cov,l=self.l,logger=self.logger,
                                power_spectra_kwargs=power_spectra_kwargs,
                                cov_utils=self.cov_utils,window_l=self.window_l,
                                z_PS=z_PS,nz_PS=nz_PS,log_z_PS=log_z_PS,
                                z_PS_max=z_PS_max)
                        #FIXME: Need a dict for these args

        self.l_bins=l_bins
        self.stack_data=stack_data
        self.theta_bins=theta_bins
        self.bin_utils=None

        self.bin_cl=bin_cl
        self.bin_xi=bin_xi
        self.set_bin_params()
        self.cov_indxs=[]
        self.corr_indxs={}
        self.m1_m2s={}

        self.z_bins={}
        self.z_bins['shear']=self.tracer_utils.zs_bins
        self.z_bins['kappa']=self.tracer_utils.zk_bins
        self.z_bins['galaxy']=self.tracer_utils.zg_bins
        
        self.spin={'galaxy':0,'kappa':0,'shear':2}
        
        self.tracers=()
        n_bins={}
        self.corr_indxs=corr_indxs
        for tracer in ('shear','galaxy','kappa'):
            n_bins[tracer]=0
            if self.z_bins[tracer] is not None:
                n_bins[tracer]=self.z_bins[tracer]['n_bins']
                self.tracers+=(tracer,)
            else:
                self.z_bins.pop(tracer, None)
            
            if self.corr_indxs.get((tracer,tracer)) is not None:
                continue

            self.corr_indxs[(tracer,tracer)]=[j for j in itertools.combinations_with_replacement(
                                                    np.arange(n_bins[tracer]),2)]
            
            if tracer=='galaxy' and not self.do_cov:
                self.corr_indxs[(tracer,tracer)]=[(i,i) for i in np.arange(n_bins[tracer])] #by default, no cross correlations between galaxy bins
        
        for tracer1 in self.tracers:#zbin-indexs for cross correlations
            for tracer2 in self.tracers:
                self.m1_m2s[(tracer1,tracer2)]=[(self.spin[tracer1],self.spin[tracer2])] 
                if tracer1==tracer2:
                    continue
                if self.corr_indxs.get((tracer1,tracer2)) is not None:
                    continue
                self.corr_indxs[(tracer1,tracer2)]=[ k for l in [[(i,j) for i in np.arange(
                                        n_bins[tracer1])] for j in np.arange(n_bins[tracer2])] for k in l]
                
        self.m1_m2s[('shear','shear')]=[(2,2),(2,-2)]
        self.m1_m2s[('window')]=[(0,0)]
#         print('corr_indxs',self.corr_indxs)
        
        self.stack_indxs=self.corr_indxs.copy()

        if np.isscalar(self.f_sky):
            f_temp=np.copy(self.f_sky)
            self.f_sky={}
            for kk in self.corr_indxs.keys():
#                 n_indx=len(self.corr_indxs[kk])
                indxs=self.corr_indxs[kk]
                self.f_sky[kk]={}
                self.f_sky[kk[::-1]]={}
                for idx in indxs:
                    self.f_sky[kk][idx]=f_temp #*np.ones((n_indx,n_indx))
                    self.f_sky[kk[::-1]][idx[::-1]]=f_temp
        
        self.Win={}
        self.Win=window_utils(window_l=self.window_l,l=self.l,corrs=self.corrs,m1_m2s=self.m1_m2s,\
                        use_window=use_window,do_cov=self.do_cov,cov_utils=self.cov_utils,
                        f_sky=f_sky,corr_indxs=self.corr_indxs,z_bins=self.z_bins,
                        window_lmax=self.window_lmax,Win=Win,HT=self.HT,do_xi=self.do_xi,
                        xi_win=self.xi_win,
                        xi_bin_utils=self.xi_bin_utils,store_win=store_win,wigner_files=wigner_files)
        if self.Tri_cov:
            self.CTR=cov_matter_tri(k=self.l)

    def update_zbins(self,z_bins={},tracer='shear'):
        self.tracer_utils.set_zbins(z_bins,tracer=tracer)
        self.z_bins['shear']=self.tracer_utils.zs_bins
        self.z_bins['galaxy']=self.tracer_utils.zg_bins
        self.z_bins['kappa']=self.tracer_utils.zk_bins
        return

    def set_HT(self,HT=None,HT_kwargs=None):
        self.HT=HT #We are using Wigner transforms now. Change to WT maybe?
        self.m1_m2s=self.HT.m1_m2s
        self.l=self.HT.l
        # if HT is None:
        #     if HT_kwargs is None:
        #         th_min=1./60. if theta_bins is None else np.amin(theta_bins)
        #         th_max=5 if theta_bins is None else np.amax(theta_bins)
        #         HT_kwargs={'l_min':min(l),'l_max':max(l),
        #                     'theta_min':th_min*d2r,'theta_max':th_max*d2r,
        #                     'n_zeros':2000,'prune_theta':2,'m1_m2':[(0,0)]}
        #     HT_kwargs['logger']=self.logger
        #     self.HT=hankel_transform(**HT_kwargs)
        #
        # self.l_cut_jnu={}
        # self.m1_m2s=self.HT.m1_m2s
        # self.l_cut_jnu['m1_m2s']=self.m1_m2s
        # if self.HT.name=='Hankel':
        #     self.l=np.unique(np.hstack((self.HT.l[i] for i in self.m1_m2s)))
        #     for m1_m2 in self.m1_m2s:
        #         self.l_cut_jnu[m1_m2]=np.isin(self.l,(self.HT.l[m1_m2]))

        # if self.HT.name=='Wigner':
        # self.l=self.HT.l
            # for m1_m2 in self.m1_m2s:
            #     self.l_cut_jnu[m1_m2]=np.isin(self.l,(self.l))
            # #FIXME: This is ugly

    def set_bin_params(self):
        """
            Setting up the binning functions to be used in binning the data
        """
        self.binning=binning()
        if self.bin_cl:
            self.cl_bin_utils=self.binning.bin_utils(r=self.l,r_bins=self.l_bins,
                                                r_dim=2,mat_dims=[1,2])
        self.xi_bin_utils={}
        if self.do_xi and self.bin_xi:
            for m1_m2 in self.m1_m2s:
                self.xi_bin_utils[m1_m2]=self.binning.bin_utils(r=self.HT.theta[m1_m2]/d2r,
                                                    r_bins=self.theta_bins,
                                                    r_dim=2,mat_dims=[1,2])
         
    def calc_cl(self,zs1_indx=-1, zs2_indx=-1,corr=('shear','shear')):
        """
            Compute the angular power spectra, Cl between two source bins
            zs1, zs2: Source bins. Dicts containing information about the source bins
        """

        zs1=self.z_bins[corr[0]][zs1_indx]#.copy() #we will modify these locally
        zs2=self.z_bins[corr[1]][zs2_indx]#.copy()

        clz=self.Ang_PS.clz
        cls=clz['cls']
        f=self.Ang_PS.cl_f
        sc=zs1['kernel_int']*zs2['kernel_int']

        dchi=np.copy(clz['dchi'])
#         if corr[0]=='galaxy':  #take care of different factors of c/H in different correlations. Done during kernel definition, tracer_utils
#                                 #Default is for shear. For every replacement of shear with galaxy, remove 1 factor.... Taken care of in kernel definitons.
#             dchi/=clz['cH']
#         if corr[1]=='galaxy':
#             dchi/=clz['cH']

        cl=np.dot(cls.T*sc,dchi)
                # cl*=2./np.pi #FIXME: needed to match camb... but not CCL
        return cl
    
    def cl_cov(self,cls=None, zs_indx=[],tracers=[],Win=None):
        """
            Computes the covariance between any two tomographic power spectra.
            cls: tomographic cls already computed before calling this function
            zs_indx: 4-d array, noting the indices of the source bins involved
            in the tomographic cls for which covariance is computed.
            For ex. covariance between 12, 56 tomographic cross correlations
            involve 1,2,5,6 source bins
        """
        cov={}
        cov['final']=None

        cov['G']=None
        cov['G1324_B']=None;cov['G1423_B']=None
        
        if self.use_window:
            cov['G1324'],cov['G1423']=self.cov_utils.gaussian_cov_window(cls,
                                            self.SN,tracers,zs_indx,self.do_xi,Win['cov'][tracers][zs_indx])                               
        else: 
            fs=self.f_sky
            if self.do_xi and self.xi_win: #in this case we need to use a separate function directly from xi_cov
                cov['G1324']=0
                cov['G1423']=0
            else:
                cov['G1324'],cov['G1423']=self.cov_utils.gaussian_cov(cls,
                                            self.SN,tracers,zs_indx,self.do_xi,fs)
        cov['G']=cov['G1324']+cov['G1423'] 
        cov['final']=cov['G']

        if not self.do_xi:
            cov['G1324']=None #save memory
            cov['G1423']=None

        cov['SSC']=0
        cov['Tri']=0
        
#         if not 'galaxy' in tracers: 
        if self.Tri_cov or self.SSV_cov:
            zs1=self.z_bins[tracers[0]][zs_indx[0]]
            zs2=self.z_bins[tracers[1]][zs_indx[1]]
            zs3=self.z_bins[tracers[2]][zs_indx[2]]
            zs4=self.z_bins[tracers[3]][zs_indx[3]]
#                 sig_cL=zs1['kernel_int']*zs2['kernel_int']*zs3['kernel_int']*zs4['kernel_int']
            sig_cL=zs1['Gkernel_int']*zs2['Gkernel_int']*zs3['Gkernel_int']*zs4['Gkernel_int']#Only use lensing kernel... not implemented for galaxies
            sig_cL*=self.Ang_PS.clz['dchi']

        if self.SSV_cov :
            clz=self.Ang_PS.clz
            Win_cl=None
            Om_w12=None
            Om_w34=None
            fs0=self.f_sky[tracers[0],tracers[1]][zs_indx[0],zs_indx[1]] * self.f_sky[tracers[2],tracers[3]][zs_indx[2],zs_indx[3]]
            if self.use_window:
                Win_cl=Win['cov'][tracers][zs_indx]['mask_comb_cl']
                Om_w12=Win['cov'][tracers][zs_indx]['Om_w12']
                Om_w34=Win['cov'][tracers][zs_indx]['Om_w34']
            sigma_win=self.cov_utils.sigma_win_calc(cls_lin=clz['cls_lin'],Win_cl=Win_cl,Om_w12=Om_w12,Om_w34=Om_w34)

            clr=self.Ang_PS.clz['clsR']
            if self.tidal_SSV_cov:
                clr=self.Ang_PS.clz['clsR']+ self.Ang_PS.clz['clsRK']/6.

            sig_F=np.sqrt(sig_cL*sigma_win) #kernel is function of l as well due to spin factors
            clr=clr*sig_F.T
            cov['SSC']=np.dot(clr.T,clr)

        if self.Tri_cov:
            cov['Tri']=self.CTR.cov_tri_zkernel(P=self.Ang_PS.clz['cls'],z_kernel=sig_cL/self.Ang_PS.clz['chi']**2) #FIXME: check dimensions, get correct factors of length.. chi**2 is guessed from eq. A3 of https://arxiv.org/pdf/1601.05779.pdf ... note that cls here is in units of P(k)/chi**2
            fs0=self.f_sky[tracers[0],tracers[1]][zs_indx[0],zs_indx[1]] 
            fs0*=self.f_sky[tracers[2],tracers[3]][zs_indx[2],zs_indx[3]]
            fs0=np.sqrt(fs0)
            cov['Tri']/=self.cov_utils.gaussian_cov_norm_2D*fs0 #(2l+1)f_sky.. we didnot normalize gaussian covariance in trispectrum computation.
        
        if self.use_window and (self.SSV_cov or self.Tri_cov): #Check: This is from writing p-cl as M@cl... cov(p-cl)=M@cov(cl)@M.T ... separate  M when different p-cl
            M1=Win['cl'][(tracers[0],tracers[1])][(zs_indx[0],zs_indx[1])]['M'] #12
            M2=Win['cl'][(tracers[2],tracers[3])][(zs_indx[2],zs_indx[3])]['M'] #34
            cov['final']=cov['G']+ M1@(cov['SSC']+cov['Tri'])@M2.T
        else:
            cov['final']=cov['G']+cov['SSC']+cov['Tri']

        if not self.do_xi:
            for k in ['final','G','SSC','Tri']:#no need to bin G1324 and G1423
                cl_none,cov[k+'_b']=self.bin_cl_func(cov=cov[k])
                del cov[k]
        return cov

    def bin_cl_func(self,cl=None,cov=None):
        """
            bins the tomographic power spectra
            results: Either cl or covariance
            bin_cl: if true, then results has cl to be binned
            bin_cov: if true, then results has cov to be binned
            Both bin_cl and bin_cov can be true simulatenously.
        """
        cl_b=None
        cov_b=None
        if self.bin_cl:
            if not cl is None:
                cl_b=self.binning.bin_1d(xi=cl,bin_utils=self.cl_bin_utils)
            if not cov is None:
                cov_b=self.binning.bin_2d(cov=cov,bin_utils=self.cl_bin_utils)
        return cl_b,cov_b

    def combine_cl_tomo(self,cl_compute_dict={},corr=None,Win=None):
        cl_b={}#,corr2:{}}

        for (i,j) in self.corr_indxs[corr]+self.cov_indxs:
            clij=cl_compute_dict[(i,j)]
#             if self.use_window:
#                     clij=clij@Win['cl'][corr][(i,j)]['M'] #pseudo cl.. now passed as input
            cl_b[(i,j)],cov_none=self.bin_cl_func(cl=clij,cov=None)
        return cl_b
    
    def calc_pseudo_cl(self,cl,Win,zs1_indx=-1, zs2_indx=-1,corr=('shear','shear')):
        return cl@Win['cl'][corr][(zs1_indx,zs2_indx)]['M'] #pseudo cl

    def cl_tomo(self,cosmo_h=None,cosmo_params=None,pk_params=None,pk_func=None,
                corrs=None,bias_kwargs={},bias_func=None,stack_corr_indxs=None):
        """
         Computes full tomographic power spectra and covariance, including shape noise. output is
         binned also if needed.
         Arguments are for the power spectra  and sigma_crit computation,
         if it needs to be called from here.
         source bins are already set. This function does set the sigma crit for sources.
        """

        l=self.l
        if corrs is None:
            corrs=self.corrs

        #tracers=[j for i in corrs for j in i]
        tracers=np.unique([j for i in corrs for j in i])

        corrs2=corrs.copy()
        if self.do_cov:#make sure we compute cl for all cross corrs necessary for covariance
                        #FIXME: If corrs are gg and ll only, this will lead to uncessary gl. This
                        #        is an unlikely use case though
#             corrs2=[]
            for i in np.arange(len(tracers)):
                for j in np.arange(i,len(tracers)):
                    if (tracers[i],tracers[j]) not in corrs2 and (tracers[j],tracers[i]) in corrs2:
                        corrs2+=[(tracers[i],tracers[j])]
                        print('added extra corr calc for covariance',corrs2)

        if cosmo_h is None:
            cosmo_h=self.Ang_PS.PS.cosmo_h

        self.SN={}
        # self.SN[('galaxy','shear')]={}
        if 'shear' in tracers:
#             self.lensing_utils.set_zs_sigc(cosmo_h=cosmo_h,zl=self.Ang_PS.z)
            self.tracer_utils.set_kernel(cosmo_h=cosmo_h,zl=self.Ang_PS.z,tracer='shear')
            self.SN[('shear','shear')]=self.tracer_utils.SN['shear']
        if 'kappa' in tracers:
#             self.lensing_utils.set_zs_sigc(cosmo_h=cosmo_h,zl=self.Ang_PS.z)
            self.tracer_utils.set_kernel(cosmo_h=cosmo_h,zl=self.Ang_PS.z,tracer='kappa')
            self.SN[('kappa','kappa')]=self.tracer_utils.SN['kappa']
        if 'galaxy' in tracers:
            if bias_func is None:
                bias_func='constant_bias'
                bias_kwargs={'b1':1,'b2':1}
            self.tracer_utils.set_kernel(cosmo_h=cosmo_h,zl=self.Ang_PS.z,tracer='galaxy')
            self.SN[('galaxy','galaxy')]=self.tracer_utils.SN['galaxy']

        self.Ang_PS.angular_power_z(cosmo_h=cosmo_h,pk_params=pk_params,pk_func=pk_func,
                                cosmo_params=cosmo_params)

        out={}
        cl={}
        pcl={} #pseudo_cl
        cov={}
        cl_b={}
        for corr in corrs2:
            corr2=corr[::-1]
            cl[corr]={}
            cl[corr2]={}
            pcl[corr]={}
            pcl[corr2]={}
            corr_indxs=self.corr_indxs[(corr[0],corr[1])]#+self.cov_indxs
            for (i,j) in corr_indxs:
                # out[(i,j)]
                cl[corr][(i,j)]=delayed(self.calc_cl)(zs1_indx=i,zs2_indx=j,corr=corr)
                if self.use_window:
                    pcl[corr][(i,j)]=delayed(self.calc_pseudo_cl)(cl[corr][(i,j)],Win=self.Win.Win,zs1_indx=i,zs2_indx=j,corr=corr)
                else:
                    pcl[corr][(i,j)]=cl[corr][(i,j)]
                cl[corr2][(j,i)]=cl[corr][(i,j)]#useful in gaussian covariance calculation.
                pcl[corr2][(j,i)]=pcl[corr][(i,j)]#useful in gaussian covariance calculation.
        for corr in corrs:
            cl_b[corr]=delayed(self.combine_cl_tomo)(pcl[corr],corr=corr,Win=self.Win.Win) #bin only pseudo-cl
            
        print('cl dict done')
        if self.do_cov:
            start_j=0
            corrs_iter=[(corrs[i],corrs[j]) for i in np.arange(len(corrs)) for j in np.arange(i,len(corrs))]
            for (corr1,corr2) in corrs_iter:
                cov[corr1+corr2]={}
                cov[corr2+corr1]={}

                corr1_indxs=self.corr_indxs[(corr1[0],corr1[1])]
                corr2_indxs=self.corr_indxs[(corr2[0],corr2[1])]

                if corr1==corr2:
                    cov_indxs_iter=[ k for l in [[(i,j) for j in np.arange(i,
                                     len(corr1_indxs))] for i in np.arange(len(corr2_indxs))] for k in l]
                else:
                    cov_indxs_iter=[ k for l in [[(i,j) for i in np.arange(
                                    len(corr1_indxs))] for j in np.arange(len(corr2_indxs))] for k in l]

                for (i,j) in cov_indxs_iter:
                    indx=corr1_indxs[i]+corr2_indxs[j]
                    cov[corr1+corr2][indx]=delayed(self.cl_cov)(cls=cl, zs_indx=indx,Win=self.Win.Win,
                                                                    tracers=corr1+corr2)
                    indx2=corr2_indxs[j]+corr1_indxs[i]
                    cov[corr2+corr1][indx2]=cov[corr1+corr2][indx]

        out_stack=delayed(self.stack_dat)({'cov':cov,'cl_b':cl_b,'est':'cl_b'},corrs=corrs,
                                          corr_indxs=stack_corr_indxs)
        return {'stack':out_stack,'cl_b':cl_b,'cov':cov,'cl':cl,'pseudo_cl':pcl}

    def xi_cov(self,cov_cl={},cls={},m1_m2=None,m1_m2_cross=None,clr=None,clrk=None,indxs_1=[],
               indxs_2=[],corr1=[],corr2=[], Win=None):
        """
            Computes covariance of xi, by performing 2-D hankel transform on covariance of Cl.
            In current implementation of hankel transform works only for m1_m2=m1_m2_cross.
            So no cross covariance between xi+ and xi-.
        """

        z_indx=indxs_1+indxs_2
        tracers=corr1+corr2
        if m1_m2_cross is None:
            m1_m2_cross=m1_m2
        cov_xi={}

        if self.HT.name=='Hankel' and m1_m2!=m1_m2_cross:
            n=len(self.theta_bins)-1
            cov_xi['final']=np.zeros((n,n))
            return cov_xi
        
        SN1324=0
        SN1423=0
        
        if np.all(np.array(tracers)=='shear') and not m1_m2==m1_m2_cross: #cross between xi+ and xi-
            if self.use_window:
                G1324,G1423=self.cov_utils.gaussian_cov_window(cls,self.SN,tracers,z_indx,self.do_xi,Win['cov'][tracers][z_indx],Bmode_mf=-1)
            else:
                if not self.xi_win:
                    G1324,G1423=self.cov_utils.gaussian_cov(cls,self.SN,tracers,z_indx,self.do_xi,self.f_sky,Bmode_mf=-1)
                else:
                    G1324=0
                    G1423=0
            cov_cl_G=G1324+G1423
        else:
            cov_cl_G=cov_cl['G1324']+cov_cl['G1423'] #FIXME: needs Bmode for shear

        
        if not self.use_window and self.xi_win: #This is an appproximation to account for window. Correct thing is pseudo cl covariance but it is expensive to very high l needed for proper wigner transforms.
            HT_kwargs={'l_cl':self.l,'m1_m2':m1_m2,'m1_m2_cross':m1_m2_cross}
            bf=1
            if np.all(np.array(tracers)=='shear') and not m1_m2==m1_m2_cross: #cross between xi+ and xi-
                bf=-1
            cov_xi['G']=self.cov_utils.xi_gaussian_cov_window_approx(cls,self.SN,tracers,z_indx,self.do_xi,Win['cov'][tracers][z_indx],self.HT,HT_kwargs,bf)
        else:
            th0,cov_xi['G']=self.HT.projected_covariance2(l_cl=self.l,m1_m2=m1_m2,
                                                      m1_m2_cross=m1_m2_cross,
                                                      cl_cov=cov_cl_G)


        cov_xi['G']=self.binning.bin_2d(cov=cov_xi['G'],bin_utils=self.xi_bin_utils[m1_m2])
        #binning is cheap
 
#         cov_xi['final']=cov_xi['G']
        cov_xi['SSC']=0
        cov_xi['Tri']=0

        if self.SSV_cov:
            th0,cov_xi['SSC']=self.HT.projected_covariance2(l_cl=self.l,m1_m2=m1_m2,
                                                            m1_m2_cross=m1_m2_cross,
                                                            cl_cov=cov_cl['SSC'])
            cov_xi['SSC']=self.binning.bin_2d(cov=cov_xi['SSC'],bin_utils=self.xi_bin_utils[m1_m2])
        if self.Tri_cov:
            th0,cov_xi['Tri']=self.HT.projected_covariance2(l_cl=self.l,m1_m2=m1_m2,
                                                            m1_m2_cross=m1_m2_cross,
                                                            cl_cov=cov_cl['Tri'])
            cov_xi['Tri']=self.binning.bin_2d(cov=cov_xi['Tri'],bin_utils=self.xi_bin_utils[m1_m2])
            
        cov_xi['final']=cov_xi['G']+cov_xi['SSC']+cov_xi['Tri']
        #         if self.use_window: #pseudo_cl:
        if self.use_window or self.xi_win:
            cov_xi['G']/=(Win['cl'][corr1][indxs_1]['xi_b']*Win['cl'][corr2][indxs_2]['xi_b'])
            cov_xi['final']/=(Win['cl'][corr1][indxs_1]['xi_b']*Win['cl'][corr2][indxs_2]['xi_b'])
        
        return cov_xi

    def get_xi(self,cls={},m1_m2=[],corr=None,indxs=None,Win=None):
        cl=cls[corr][indxs] #this should be pseudo-cl when using window
        th,xi=self.HT.projected_correlation(l_cl=self.l,m1_m2=m1_m2,cl=cl)
        if not self.use_window and self.xi_win: #This is an appproximation to account for window. Correct thing is pseudo cl but it is expensive to very high l needed for proper wigner transforms.
            xi=xi*Win['cl'][corr][indxs]['xi']

        xi_b=self.binning.bin_1d(xi=xi,bin_utils=self.xi_bin_utils[m1_m2])

        if self.use_window or self.xi_win:
            xi_b/=(Win['cl'][corr][indxs]['xi_b']) 
        return xi_b

    def xi_tomo(self,cosmo_h=None,cosmo_params=None,pk_params=None,pk_func=None,
                corrs=None):
        """
            Computed tomographic angular correlation functions. First calls the tomographic
            power spectra and covariance and then does the hankel transform and  binning.
        """
        """
            For hankel transform is done on l-theta grid, which is based on m1_m2. So grid is
            different for xi+ and xi-.
            In the init function, we combined the ell arrays for all m1_m2. This is not a problem
            except for the case of SSV, where we will use l_cut to only select the relevant values
        """

        if cosmo_h is None:
            cosmo_h=self.Ang_PS.PS.cosmo_h
        if corrs is None:
            corrs=self.corrs

        #Donot use delayed here. Leads to error/repeated calculations
        cls_tomo_nu=self.cl_tomo(cosmo_h=cosmo_h,cosmo_params=cosmo_params,
                            pk_params=pk_params,pk_func=pk_func,
                            corrs=corrs)

        cl=cls_tomo_nu['pseudo_cl'] #Note that if window is turned off, pseudo_cl=cl
        cov_xi={}
        xi={}
        out={}
        self.clr={}
        # for m1_m2 in self.m1_m2s:
        for corr in corrs:
            m1_m2s=self.m1_m2s[corr]
            xi[corr]={}
            for im in np.arange(len(m1_m2s)):
                m1_m2=m1_m2s[im]
                xi[corr][m1_m2]={}
                for indx in self.corr_indxs[corr]:
                    xi[corr][m1_m2][indx]=delayed(self.get_xi)(cls=cl,corr=corr,indxs=indx,
                                                        m1_m2=m1_m2,Win=self.Win.Win)
        if self.do_cov:
            for corr1 in corrs:
                for corr2 in corrs:

                    m1_m2s_1=self.m1_m2s[corr1]
                    indxs_1=self.corr_indxs[corr1]
                    m1_m2s_2=self.m1_m2s[corr2]
                    indxs_2=self.corr_indxs[corr2]

                    corr=corr1+corr2
                    cov_xi[corr]={}

                    for im1 in np.arange(len(m1_m2s_1)):
                        m1_m2=m1_m2s_1[im1]
                        # l_cut=self.l_cut_jnu[m1_m2]
                        cov_cl=cls_tomo_nu['cov'][corr]#.compute()
                        clr=None
                        if self.SSV_cov:
                            clr=self.Ang_PS.clz['clsR']#[:,l_cut]#this is mainly for Hankel transform.
                                                                # Which doesnot work for cross correlations
                                                                # Does not impact Wigner.

                            if self.tidal_SSV_cov:
                                clr+=self.Ang_PS.clz['clsRK']/6#[:,l_cut].

                        start2=0
                        if corr1==corr2:
                            start2=im1
                        for im2 in np.arange(start2,len(m1_m2s_2)):
                            m1_m2_cross=m1_m2s_2[im2]
                            cov_xi[corr][m1_m2+m1_m2_cross]={}

                            for i1 in np.arange(len(indxs_1)):
                                start2=0
                                if corr1==corr2:# and m1_m2==m1_m2_cross:
                                    start2=i1
                                for i2 in np.arange(start2,len(indxs_2)):
                                    indx=indxs_1[i1]+indxs_2[i2]
                                    cov_xi[corr][m1_m2+m1_m2_cross][indx]=delayed(self.xi_cov)(
                                                                    cov_cl=cov_cl[indx]#.compute()
                                                                    , cls=cl
                                                                    ,m1_m2=m1_m2,
                                                                    m1_m2_cross=m1_m2_cross,clr=clr,
                                                                    Win=self.Win.Win,
                                                                    indxs_1=indxs_1[i1],
                                                                    indxs_2=indxs_2[i2],
                                                                    corr1=corr1,corr2=corr2
                                                                    )
        out['stack']=delayed(self.stack_dat)({'cov':cov_xi,'xi':xi,'est':'xi'},corrs=corrs)
        out['xi']=xi
        out['cov']=cov_xi
        out['cl']=cls_tomo_nu
        return out


    def stack_dat(self,dat,corrs,corr_indxs=None):
        """
            outputs from tomographic caluclations are dictionaries.
            This fucntion stacks them such that the cl or xi is a long
            1-d array and the covariance is N X N array.
            dat: output from tomographic calculations.
            XXX: reason that outputs tomographic bins are distionaries is that
            it make is easier to
            handle things such as binning, hankel transforms etc. We will keep this structure for now.
        """

        if corr_indxs is None:
            corr_indxs=self.stack_indxs

        est=dat['est']
        if est=='xi':
            len_bins=len(self.theta_bins)-1
        else:
            #est='cl_b'
            n_m1_m2=1
            if self.l_bins is not None:
                len_bins=len(self.l_bins)-1
            else:
                len_bins=len(self.l)

        n_bins=0
        for corr in corrs:
            if est=='xi':
                n_m1_m2=len(self.m1_m2s[corr])
            n_bins+=len(corr_indxs[corr])*n_m1_m2 #np.int64(nbins*(nbins-1.)/2.+nbins)
#         print(n_bins,len_bins,n_m1_m2)
        D_final=np.zeros(n_bins*len_bins)

        i=0
        for corr in corrs:
            n_m1_m2=1
            if est=='xi':
                m1_m2=self.m1_m2s[corr]
                n_m1_m2=len(m1_m2)

            for im in np.arange(n_m1_m2):
                if est=='xi':
                    dat_c=dat[est][corr][m1_m2[im]]
                else:
                    dat_c=dat[est][corr]#[corr] #cl_b gets keys twice. dask won't allow standard dict merge.. should be fixed

                for indx in corr_indxs[corr]:
                    D_final[i*len_bins:(i+1)*len_bins]=dat_c[indx]
                    i+=1

        if not self.do_cov:
            out={'cov':None}
            out[est]=D_final
            return out

        cov_final=np.zeros((len(D_final),len(D_final)))-999.#np.int(nD2*(nD2+1)/2)

        indx0_c1=0
        for ic1 in np.arange(len(corrs)):
            corr1=corrs[ic1]
            indxs_1=corr_indxs[corr1]
            n_indx1=len(indxs_1)
            # indx0_c1=(ic1)*n_indx1*len_bins

            indx0_c2=indx0_c1
            for ic2 in np.arange(ic1,len(corrs)):
                corr2=corrs[ic2]
                indxs_2=corr_indxs[corr2]
                n_indx2=len(indxs_2)
                # indx0_c2=(ic2)*n_indx2*len_bins

                corr=corr1+corr2
                n_m1_m2_1=1
                n_m1_m2_2=1
                if est=='xi':
                    m1_m2_1=self.m1_m2s[corr1]
                    m1_m2_2=self.m1_m2s[corr2]
                    n_m1_m2_1=len(m1_m2_1)
                    n_m1_m2_2=len(m1_m2_2)

                for im1 in np.arange(n_m1_m2_1):
                    start_m2=0
                    if corr1==corr2:
                        start_m2=im1
                    for im2 in np.arange(start_m2,n_m1_m2_2):
                        indx0_m1=(im1)*n_indx1*len_bins
                        indx0_m2=(im2)*n_indx2*len_bins
                        for i1 in np.arange(n_indx1):
                            start2=0
                            if corr1==corr2:
                                start2=i1
                            for i2 in np.arange(start2,n_indx2):
                                indx0_1=(i1)*len_bins
                                indx0_2=(i2)*len_bins
                                indx=indxs_1[i1]+indxs_2[i2]

                                if est=='xi':
                                    cov_here=dat['cov'][corr][m1_m2_1[im1]+m1_m2_2[im2]][indx]['final']
                                else:
                                    cov_here=dat['cov'][corr][indx]['final_b']

                                # if im1==im2:
                                i=indx0_c1+indx0_1+indx0_m1
                                j=indx0_c2+indx0_2+indx0_m2

                                cov_final[i:i+len_bins,j:j+len_bins]=cov_here
                                cov_final[j:j+len_bins,i:i+len_bins]=cov_here.T

                                if im1!=im2 and corr1==corr2:
                                    # i=indx0_c1+indx0_1+indx0_m1
                                    # j=indx0_c2+indx0_2+indx0_m2
                                    # cov_final[i:i+len_bins,j:j+len_bins]=cov_here
                                    # cov_final[j:j+len_bins,i:i+len_bins]=cov_here.T

                                    i=indx0_c1+indx0_1+indx0_m2
                                    j=indx0_c2+indx0_2+indx0_m1
                                    cov_final[i:i+len_bins,j:j+len_bins]=cov_here.T
                                    cov_final[j:j+len_bins,i:i+len_bins]=cov_here

                indx0_c2+=n_indx2*len_bins*n_m1_m2_2
            indx0_c1+=n_indx1*len_bins*n_m1_m2_1

        out={'cov':cov_final}
        out[est]=D_final
        return out


if __name__ == "__main__":
    import cProfile
    import pstats

    import dask,dask.multiprocessing
    dask.config.set(scheduler='processes')
    # dask.config.set(scheduler='synchronous')  # overwrite default with single-threaded scheduler..
                                            # Works as usual single threaded worload. Useful for profiling.


    zs_bin1=source_tomo_bins(zp=[1],p_zp=np.array([1]),ns=26)

    lmax_cl=5000
    lmin_cl=2
    l_step=3 #choose odd number

#     l=np.arange(lmin_cl,lmax_cl,step=l_step) #use fewer ell if lmax_cl is too large
    l0=np.arange(lmin_cl,lmax_cl)

    lmin_clB=lmin_cl+10
    lmax_clB=lmax_cl-10
    Nl_bins=40
    l_bins=np.int64(np.logspace(np.log10(lmin_clB),np.log10(lmax_clB),Nl_bins))
    l=np.unique(np.int64(np.logspace(np.log10(lmin_cl),np.log10(lmax_cl),Nl_bins*30)))

    bin_xi=True
    theta_bins=np.logspace(np.log10(1./60),1,20)


    do_cov=True
    bin_cl=True
    SSV_cov=True
    tidal_SSV_cov=True
    stack_data=True

    # kappa0=lensing_lensing(zs_bins=zs_bin1,do_cov=do_cov,bin_cl=bin_cl,l_bins=l_bins,l=l0,
    #            stack_data=stack_data,SSV_cov=SSV_cov,tidal_SSV_cov=tidal_SSV_cov,)
    #
    # cl_G=kappa0.kappa_cl_tomo() #make the compute graph
    # cProfile.run("cl0=cl_G['stack'].compute()",'output_stats_1bin')
    # cl=cl0['cl']
    # cov=cl0['cov']
    #
    # p = pstats.Stats('output_stats_1bin')
    # p.sort_stats('tottime').print_stats(10)

##############################################################
    do_xi=True
    bin_cl=not do_xi
    zmin=0.3
    zmax=2
    z=np.linspace(0,5,200)
    pzs=lsst_pz_source(z=z)
    x=z<zmax
    x*=z>zmin
    z=z[x]
    pzs=pzs[x]

    ns0=26#+np.inf # Total (cumulative) number density of source galaxies, arcmin^-2.. setting to inf turns off shape noise
    nbins=5 #number of tomographic bins
    z_sigma=0.01
    zs_bins=source_tomo_bins(zp=z,p_zp=pzs,ns=ns0,nz_bins=nbins,
                             ztrue_func=ztrue_given_pz_Gaussian,
                             zp_bias=np.zeros_like(z),
                            zp_sigma=z_sigma*np.ones_like(z))


    if not do_xi:
        kappaS = lensing_lensing(zs_bins=zs_bins,l=l,do_cov=do_cov,bin_cl=bin_cl,l_bins=l_bins,
                    stack_data=stack_data,SSV_cov=SSV_cov,
                    tidal_SSV_cov=tidal_SSV_cov,do_xi=do_xi,bin_xi=bin_xi,
                    theta_bins=theta_bins)#ns=np.inf)
        clSG=kappaS.kappa_cl_tomo()#make the compute graph
        cProfile.run("cl0=clSG['stack'].compute(num_workers=4)",'output_stats_3bins')
        cl=cl0['cl']
        cov=cl0['cov']
    else:
        l_max=2e4
        l_W=np.arange(2,l_max,dtype='int')
        WT_kwargs={'l':l_W ,'theta': np.logspace(-1,1,200)*d2r,'m1_m2':[(2,2),(2,-2)]}
        cProfile.run("WT=wigner_transform(**WT_kwargs)",'output_stats_3bins')
        kappaS = lensing_lensing(zs_bins=zs_bins,l=l,do_cov=do_cov,bin_cl=bin_cl,l_bins=l_bins,
                    stack_data=stack_data,SSV_cov=SSV_cov,HT=WT,
                    tidal_SSV_cov=tidal_SSV_cov,do_xi=do_xi,bin_xi=bin_xi,
                    theta_bins=theta_bins)#ns=np.inf)
        xiSG=kappaS.xi_tomo()#make the compute graph
        cProfile.run("xi0=xiSG['stack'].compute(num_workers=4)",'output_stats_3bins')


    p = pstats.Stats('output_stats_3bins')
    p.sort_stats('tottime').print_stats(10)
