date - time has non meaning, it is used because the code is adjusted to run both datetime and with integer hours simulations
t1, t2 are the start and end times - it is possible to use any date but hours are important due to electricy tariffs
some constraints (pumps availability, vsp_volume, vsp_changes_limit) are set according to datetime - it is required to pass dates accordint to the respective time of the simulation

vsp - variable speed pumps
vsp - the cost function does not take the energy consumption of the vsp into account - vsp file power column = 0 (Selek, 2012)

vsp4 constant flow - forcing the solution to the required constant flow by setting min and max flows to the same value (vsp.csv) 
vsp volume constraints - min and max volumes to different periods are set in vsp_volume.csv file