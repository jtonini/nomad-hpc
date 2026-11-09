1
 program ciudg      
 multireference single and double excitation configuration
 interaction based on the graphical unitary group approach.


 ************************************************************************
 beginning the bk-type iterative procedure (nzcsf=     1)...
 ************************************************************************

   iter      root         energy      deltae       apxde    residual       rtol
        ---- ----   --------------  ----------  ----------  ----------  ----------
 mr-sdci #  1  1    -76.0253553485  0.0000E+00  2.5320E-01  9.9888E-01  1.0000E-04   

 mr-sdci  convergence not reached after  1 iterations.

 final mr-sdci  convergence information:
 mr-sdci #  1  1    -76.0253553485  0.0000E+00  2.5320E-01  9.9888E-01  1.0000E-04   

 from bk iterations: iconv=   1

 ************************************************************************
 beginning the ci iterative diagonalization procedure... 
 ************************************************************************

   iter      root         energy      deltae       apxde    residual       rtol
        ---- ----   --------------  ----------  ----------  ----------  ----------
 mr-sdci #  1  1    -76.2243425459  1.9899E-01  6.3980E-03  1.5657E-01  1.0000E-04   
 mr-sdci #  2  1    -76.2302857566  5.9432E-03  2.3177E-04  2.8767E-02  1.0000E-04   
 mr-sdci #  3  1    -76.2305262981  2.4054E-04  1.4783E-05  6.5008E-03  1.0000E-04   
 mr-sdci #  4  1    -76.2305392724  1.2974E-05  7.9710E-07  1.6330E-03  1.0000E-04   
 mr-sdci #  5  1    -76.2305400583  7.8593E-07  2.9843E-08  3.0692E-04  1.0000E-04   
 mr-sdci #  6  1    -76.2305400880  2.9615E-08  1.9074E-09  7.7364E-05  1.0000E-04   

 mr-sdci  convergence criteria satisfied after  6 iterations.

 final mr-sdci  convergence information:
 mr-sdci #  6  1    -76.2305400880  2.9615E-08  1.9074E-09  7.7364E-05  1.0000E-04   

 number of reference csfs (nref) is     1.  root number (iroot) is  1.
 c0**2 =   0.95069902  c**2 (all zwalks) =   0.95069902

 eref      =    -76.025355348458   "relaxed" cnot**2         =   0.950699023359
 eci       =    -76.230540087951   deltae = eci - eref       =  -0.205184739493
 eci+dv1   =    -76.240655896000   dv1 = (1-cnot**2)*deltae  =  -0.010115808049
 eci+dv2   =    -76.241180477602   dv2 = dv1 / cnot**2       =  -0.010640389651
 eci+dv3   =    -76.241762442033   dv3 = dv1 / (2*cnot**2-1) =  -0.011222354082
 eci+pople =    -76.239321738843   ( 10e- scaled deltae )    =  -0.213966390385
