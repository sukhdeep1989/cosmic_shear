import os,sys

from power_spectra import *
from angular_power_spectra import *
from hankel_transform import *
from binning import *
from astropy.constants import c,G
from astropy import units as u
import numpy as np
import torch as tc
from scipy.interpolate import interp1d
from scipy.integrate import quad as scipy_int1d

d2r=np.pi/180.
c=c.to(u.km/u.second)

class Lensing_utils():
    def __init__(self,sigma_gamma=0.3,zs_bins=None,logger=None):
        #self.ns=ns
        ns=1 #different for different z_source. should be property of z_source
        self.sigma_gamma=sigma_gamma
        self.logger=logger
        self.SN0=sigma_gamma**2/(ns*3600./d2r**2)
        #Gravitaional const to get Rho crit in right units
        self.G2=G.to(u.Mpc/u.Msun*u.km**2/u.second**2)
        self.G2*=8*np.pi/3.
        if zs_bins is not None: #sometimes we call this class just to access some of the functions
            self.zs_bins=zs_bins
            self.set_shape_noise()

    def Rho_crit(self,cosmo_h=None):
        #G2=G.to(u.Mpc/u.Msun*u.km**2/u.second**2)
        #rc=3*cosmo_h.H0**2/(8*np.pi*G2)
        rc=cosmo_h.H0**2/(self.G2) #factors of pi etc. absorbed in self.G2
        rc=rc.to(u.Msun/u.pc**2/u.Mpc)# unit of Msun/pc^2/mpc
        return tc.tensor(rc.value,dtype=tc.double)

    def sigma_crit(self,zl=[],zs=[],cosmo_h=None):
        ds=tc.tensor(cosmo_h.comoving_transverse_distance(zs))
        dl=tc.tensor(cosmo_h.comoving_transverse_distance(zl))
        ddls=1.-tc.ger(1./ds,dl) #(ds-dl)/ds
        w=(3./2.)*((cosmo_h.H0/c)**2)*(1+zl)*dl/self.Rho_crit(cosmo_h)
        sigma_c=1./(ddls*tc.tensor(w))
        x=ddls<=0 #zs<zl
        sigma_c[x]=np.inf
        return sigma_c

    def shape_noise_calc(self,zs1=None,zs2=None):
        if not tc.all(zs1['z'].eq(zs2['z'])):
            return 0
        if tc.any(zs1['nz']==float('inf')) or tc.any(zs2['nz']==float('inf')):
            return 0
        SN=self.SN0*(zs1['W']*zs2['W']*zs1['nz']).sum() #FIXME: this is probably wrong.
        #Assumption: ns(z)=ns*pzs*dzs
        SN/=(zs1['nz']*zs1['W']).sum()
        SN/=(zs2['nz']*zs2['W']).sum()
        return tc.tensor(SN,dtype=tc.double)
        # XXX Make sure pzs are properly normalized


    def set_shape_noise(self,cross_PS=True):
        """
            Setting source redshift bins in the format used in code.
            Need
            zs (array): redshift bins for every source bin. if z_bins is none, then dictionary with
                        with values for each bin
            pzs: redshift distribution. same format as zs
            z_bins: if zs and pzs are for whole survey, then bins to divide the sample. If
                    tomography is based on lens redshift, then this arrays contains those redshifts.
            ns: The number density for each bin to compute shape noise.
        """
        self.ns_bins=self.zs_bins['n_bins']
        self.SN=tc.zeros((1,self.ns_bins,self.ns_bins),dtype=tc.double) #if self.do_cov else None

        for i in tc.arange(self.ns_bins):
            i=i.item()
            self.zs_bins[i]['SN']=self.shape_noise_calc(zs1=self.zs_bins[i],
                                                                    zs2=self.zs_bins[i])
            self.SN[:,i,i]=self.zs_bins[i]['SN']

        if not cross_PS: # if we are not computing cross_PS, then we assume that sources overlap in different bins. Hence we need to compute the cross shape noise
            for i in tc.arange(self.ns_bins):
                for j in tc.arange(i,self.ns_bins):
                    self.SN[:,i,j]=self.shape_noise_calc(zs1=self.zs_bins[i],
                                                                        zs2=self.zs_bins[j])
                    self.SN[:,j,i]=self.SN[:,i,j]
                    #FIXME: this shape noise calc is probably wrong

    def set_zs_sigc(self,cosmo_h=None,zl=None):
        """
            Compute rho/Sigma_crit for each source bin at every lens redshift where power spectra is computed.
            cosmo_h: cosmology to compute Sigma_crit
        """
        #We need to compute these only once in every run
        # i.e not repeat for every ij combo

        rho=self.Rho_crit(cosmo_h=cosmo_h)*cosmo_h.Om0

        for i in tc.arange(self.ns_bins):
            i=i.item()
            self.zs_bins[i]['kernel']=rho/self.sigma_crit(zl=zl,
                                                        zs=self.zs_bins[i]['z'],
                                                        cosmo_h=cosmo_h)
            self.zs_bins[i]['kernel_int']=(self.zs_bins[i]['pzdz']*self.zs_bins[i]['kernel'].transpose(1,0)).sum(1)
            self.zs_bins[i]['kernel_int']/=self.zs_bins[i]['Norm']

    def reset_zs(self):
        """
            Reset cosmology dependent values for each source bin
        """
        for i in tc.arange(self.ns_bins):
            self.zs_bins[i]['kernel']=None
            self.zs_bins[i]['kernel_int']=None
