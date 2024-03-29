import pandas as pd
import numpy as np
import operator
from scipy import sparse

from rsome import ro
import rsome as rso
from rsome import grb_solver as grb

# local imports
from . import uncertainty_utils as uutils

# solvers info
GRB_STATUS = {1: 'LOADED', 2: 'OPTIMAL', 3: 'INFEASIBLE', 4: 'INF_OR_UNBD', 5: 'UNBOUNDED'}
CLP_STATUS = {-1: 'unknown', 0: 'OPTIMAL', 1: 'primal infeasible', 2: 'dual infeasible',
              3: 'stopped on iterations or time', 4: 'stopped due to errors', 5: 'stopped by event handler'}


class ARO:
    def __init__(self, sim, robustness, lou, uset_type, worst_case=False):
        self.sim = sim
        self.robustness = robustness
        self.lou = lou  # level of uncertainty
        self.uset_type = uset_type
        self.worst_case = worst_case  # If True objective value for worst_case else nominal

        self.udata = uutils.init_uncertainty(self.sim)
        self.utanks = list(self.udata['demand'].elements.keys())
        self.nominal_demands = self.get_nominal_demands()

        self.model = ro.Model()
        self.x = self.declare_variables()
        self.z, self.z_set = self.declare_random_variables()
        self.adapt_ldr()

    def build(self):
        self.range_constraints()
        self.one_comb_only()
        self.build_mass_balance()
        self.vsp_volume()
        self.vsp_changes()
        # self.max_power()
        self.objective_func()

    def get_nominal_demands(self):
        return np.vstack([t.vars['demand'].values for t in self.sim.network.tanks.values() if t.name in self.utanks]).T

    def declare_variables(self):
        n = sum([len(s.combs) for s in self.sim.network.pump_stations.values()])
        n += len(self.sim.network.vsp.items())
        n += len(self.sim.network.wells.items())
        n += sum([len(cv.combs) for cv in self.sim.network.control_valves.values()])
        n += len(self.sim.network.valves.items())
        x = self.model.ldr(n * self.sim.num_steps)
        return x

    def declare_random_variables(self):
        z = self.model.rvar(self.nominal_demands.shape)
        z_set = rso.norm(z.reshape(-1), self.uset_type) <= self.robustness
        return z, z_set

    def get_x_idx(self, name):
        xmin_idx = self.sim.vars.loc[self.sim.vars['name'] == name].index.min()
        xmax_idx = self.sim.vars.loc[self.sim.vars['name'] == name].index.max() + 1
        return xmin_idx, xmax_idx

    def adapt_ldr(self):
        for element_name in self.sim.vars['name'].unique():
            element = self.sim.network[element_name]
            xmin_idx, xmax_idx = self.get_x_idx(element_name)

            if hasattr(element, 'combs'):
                c = len(element.combs)
            else:
                c = 1

            # adaption rule - all to all
            for t in range(1, self.sim.num_steps):
                self.x[xmin_idx + t * c: xmin_idx + (t + 1) * c].adapt(self.z[:t, :])

    def range_constraints(self):
        for s_name, s in self.sim.network.pump_stations.items():
            xmin_idx, xmax_idx = self.get_x_idx(s_name)
            self.model.st((self.x[xmin_idx: xmax_idx] >= 0).forall(self.z_set))
            self.model.st((self.x[xmin_idx: xmax_idx] <= 1).forall(self.z_set))

        for vsp_name, vsp in self.sim.network.vsp.items():
            xmin_idx, xmax_idx = self.get_x_idx(vsp_name)
            self.model.st((self.x[xmin_idx: xmax_idx] >= vsp.min_flow).forall(self.z_set))
            self.model.st((self.x[xmin_idx: xmax_idx] <= vsp.max_flow).forall(self.z_set))
            if not np.isnan(vsp.init_flow):
                self.model.st((self.x[xmin_idx] == vsp.init_flow).forall(self.z_set))

        for well_name, well in self.sim.network.wells.items():
            xmin_idx, xmax_idx = self.get_x_idx(well_name)
            self.model.st((self.x[xmin_idx: xmax_idx] >= 0).forall(self.z_set))
            self.model.st((self.x[xmin_idx: xmax_idx] <= 1).forall(self.z_set))

        for cv_name, cv in self.sim.network.control_valves.items():
            xmin_idx, xmax_idx = self.get_x_idx(cv_name)
            self.model.st((self.x[xmin_idx: xmax_idx] >= 0).forall(self.z_set))
            self.model.st((self.x[xmin_idx: xmax_idx] <= 1).forall(self.z_set))

        for v_name, v in self.sim.network.valves.items():
            xmin_idx, xmax_idx = self.get_x_idx(v_name)
            self.model.st((self.x[xmin_idx: xmax_idx] >= 0).forall(self.z_set))
            self.model.st((self.x[xmin_idx: xmax_idx] <= 1).forall(self.z_set))

    def comb_matrix(self, element, inout, param=None):
        """ return a matrix for elements with discrete hydraulic states (pumps combinations) """
        flow_direction = {'in': 1, 'out': -1}
        matrix = np.zeros([self.sim.num_steps, self.sim.num_steps * len(element.combs)])
        rows = np.hstack([np.repeat(i, len(element.combs)) for i in range(self.sim.num_steps)])
        cols = np.arange(len(element.combs) * self.sim.num_steps)
        matrix[rows, cols] = flow_direction[inout]
        if param is not None:
            matrix = matrix * element.vars[param].to_numpy()
        return matrix

    def cumulative_comb_matrix(self, element, inout, param=None):
        flow_direction = {'in': 1, 'out': -1}
        matrix = np.zeros([self.sim.num_steps, self.sim.num_steps * len(element.combs)])
        rows = np.hstack([np.repeat(i - 1, len(element.combs) * i) for i in range(1, self.sim.num_steps + 1)])
        cols = np.hstack([np.arange(len(element.combs) * (t + 1)) for t in range(self.sim.num_steps)])
        matrix[rows, cols] = flow_direction[inout]
        if param is not None:
            matrix = matrix * element.vars[param].to_numpy()
        return matrix

    def not_comb_matrix(self, inout):
        """ return a matrix for elements with continuous variables (valves, vsp, tanks) """
        flow_direction = {'in': 1, 'out': -1}
        matrix = flow_direction[inout] * np.diag(np.ones(self.sim.num_steps))
        return matrix

    def cumulative_not_comb_matrix(self, inout):
        flow_direction = {'in': 1, 'out': -1}
        matrix = np.zeros((self.sim.num_steps, self.sim.num_steps))
        matrix[np.tril_indices(self.sim.num_steps)] = flow_direction[inout]
        return matrix

    def one_comb_only(self):
        for station_name, station in self.sim.network.comb_elements.items():
            idx = self.sim.vars.loc[self.sim.vars['name'] == station.name].index
            x = self.x[idx]

            matrix = np.zeros([self.sim.num_steps, self.sim.num_steps * len(station.combs)])
            rows = np.hstack([np.repeat(i, len(station.combs)) for i in range(self.sim.num_steps)])
            cols = np.arange(len(station.combs) * self.sim.num_steps)
            matrix[rows, cols] = 1
            matrix = sparse.csr_matrix(matrix)
            self.model.st((matrix @ x <= (np.ones((self.sim.num_steps, 1)))).forall(self.z_set))

    def get_cumulative_flow_matrix(self, element_name, flow_direction):
        element = self.sim.network.flow_elements[element_name]

        if hasattr(element, 'combs'):
            mat = self.cumulative_comb_matrix(element, flow_direction, param='flow')
        else:
            mat = self.cumulative_not_comb_matrix(flow_direction)

        return mat

    def mass_balance(self, tanks: dict, affine_map=None):
        T = self.sim.num_steps
        ntanks = len(tanks)
        if ntanks == 0:
            return

        lhs = np.zeros((ntanks * T, self.x.shape[0]), dtype=float)
        lhs_init = np.zeros((ntanks * T, 1), dtype=float)
        rhs_min = np.zeros((ntanks * T, 1), dtype=float)
        rhs_max = np.zeros((ntanks * T, 1), dtype=float)
        dem = np.zeros((ntanks * T, 1), dtype=float)

        for tank_idx, (tank_name, tank) in enumerate(tanks.items()):
            dem[tank_idx * T: (tank_idx + 1) * T] = tank.vars['demand'].cumsum().values.reshape(-1, 1)
            rhs_min[tank_idx * T: (tank_idx + 1) * T] = np.append(tank.min_vol[1:], tank.final_vol).reshape(-1, 1)
            rhs_max[tank_idx * T: (tank_idx + 1) * T] = tank.max_vol
            lhs_init[tank_idx * T: (tank_idx + 1) * T] = tank.initial_vol

            for element_name in self.sim.vars['name'].unique():
                element = self.sim.network.flow_elements[element_name]
                xmin_idx, xmax_idx = self.get_x_idx(element_name)

                if element in tank.inflows:
                    mat = self.get_cumulative_flow_matrix(element_name, flow_direction='in')
                elif element in tank.pumps_outflows + tank.vsp_outflows + tank.v_outflows + tank.cv_outflows:
                    mat = self.get_cumulative_flow_matrix(element_name, flow_direction='out')
                else:
                    continue

                lhs[tank_idx * T: (tank_idx + 1) * T, xmin_idx: xmax_idx] = mat

        if affine_map is None:
            self.model.st((lhs @ self.x <= np.squeeze(rhs_max + dem - lhs_init)).forall(self.z_set))
            self.model.st((lhs @ self.x >= np.squeeze(rhs_min + dem - lhs_init)).forall(self.z_set))

        else:
            self.model.st((lhs @ self.x <= np.squeeze(rhs_max)
                           + (1 + self.lou * self.z.reshape(-1)) * np.squeeze(dem)
                           # + affine_map @ self.z.reshape(-1)
                           - np.squeeze(lhs_init)).forall(self.z_set))

            self.model.st((lhs @ self.x >= np.squeeze(rhs_min)
                           + (1 + self.lou * self.z.reshape(-1)) * np.squeeze(dem)
                           # + affine_map @ self.z.reshape(-1)
                           - np.squeeze(lhs_init)).forall(self.z_set))

    def build_mass_balance(self):
        if 'demand' in self.udata:
            utanks = {name: t for name, t in self.sim.network.tanks.items() if name in self.udata['demand'].elements}
            delta = self.udata['demand'].delta
            self.mass_balance(utanks, affine_map=delta)
        else:
            utanks = {}

        dtanks = {name: t for name, t in self.sim.network.tanks.items() if name not in utanks}
        self.mass_balance(dtanks)

    def vsp_volume(self):
        operators = {'le': operator.le, 'ge': operator.ge, 'eq': operator.eq}
        df = pd.read_csv(self.sim.data_folder + '/vsp_volume.csv')
        for i, row in df.iterrows():
            start = pd.to_datetime(row['start'], dayfirst=True)
            end = pd.to_datetime(row['end'], dayfirst=True)
            vsp_name = row['vsp']
            volume = row['vol']
            operator_type = row['constraint_type']

            vsp = self.sim.vars.loc[self.sim.vars['name'] == vsp_name, 'network_element'].values[0]
            xmin_idx, xmax_idx = self.get_x_idx(vsp_name)
            vsp_x = self.x[xmin_idx: xmax_idx]

            vsp.vars['aux'] = 0
            mask = ((vsp.vars.index >= start) & (vsp.vars.index <= end))
            vsp.vars.loc[mask, 'aux'] = 1

            matrix = self.not_comb_matrix('in')
            matrix = np.multiply(matrix, vsp.vars['aux'].to_numpy()[:, np.newaxis])  # row-wise multiplication
            lhs = matrix.sum(axis=0) @ vsp_x  # sum of matrix rows to get the total flow for period
            self.model.st((operators[operator_type](lhs, volume)).forall(self.z_set))
            vsp.vars = vsp.vars.drop('aux', axis=1)

    def vsp_changes(self):
        df = pd.read_csv(self.sim.data_folder + '/vsp_changes.csv')
        for i, row in df.iterrows():
            start = pd.to_datetime(row['start'], dayfirst=True)
            end = pd.to_datetime(row['end'], dayfirst=True)
            vsp_name = row['vsp']

            vsp = self.sim.vars.loc[self.sim.vars['name'] == vsp_name, 'network_element'].values[0]
            xmin_idx, xmax_idx = self.get_x_idx(vsp_name)
            vsp_x = self.x[xmin_idx: xmax_idx]

            vsp.vars['aux'] = 0
            mask = ((vsp.vars.index > start) & (vsp.vars.index < end))
            vsp.vars.loc[mask, 'aux'] = 1

            matrix = np.diag(np.ones(self.sim.num_steps))
            rows, cols = np.indices((self.sim.num_steps, self.sim.num_steps))
            row_vals = np.diag(rows, k=-1)
            col_vals = np.diag(cols, k=-1)
            matrix[row_vals, col_vals] = -1
            matrix[0, 0] = 0

            matrix = np.multiply(matrix, vsp.vars['aux'].to_numpy()[:, np.newaxis])  # row-wise multiplication
            matrix = sparse.csr_matrix(matrix)
            self.model.st((matrix @ vsp_x == 0).forall(self.z_set))

    def max_power(self):
        df = pd.read_csv(self.sim.data_folder + '/power_stations.csv')
        for i, row in df.iterrows():
            ps_name = row['name']
            ps = self.sim.network.power_stations[ps_name]

            lhs = 0
            for s in ps.elements:
                matrix = self.comb_matrix(s, 'in', 'power')

                xmin_idx, xmax_idx = self.get_x_idx(s.name)
                x = self.x[xmin_idx: xmax_idx]
                b = self.model.dvar(shape=xmax_idx - xmin_idx, vtype='B')
                self.model.st(x - b <= 0)
                lhs = lhs + matrix @ b

            rhs = self.sim.date_tariff['name']
            rhs = np.where(rhs == 'ON', row['max_power_on'], row['max_power_off'])
            self.model.st(lhs <= rhs)

    def objective_func(self):
        obj = 0
        for element_name, element in self.sim.network.cost_elements.items():
            xmin_idx, xmax_idx = self.get_x_idx(element_name)
            obj += (element.vars['cost'].values @ self.x[xmin_idx: xmax_idx]).sum()

        if self.worst_case:
            self.model.minmax(obj)

        if not self.worst_case:
            nominal_uset = (self.z == np.zeros(self.z.shape))
            self.model.minmax(obj, nominal_uset)

    def solve(self):
        self.model.solve(grb, display=False, params={'LogToConsole': 0, 'CSClientLog': 3})
        obj, x, status = self.model.get(), self.model.solution.x, self.model.solution.status
        status = GRB_STATUS[status]
        self.get_results()
        return status, obj

    def get_ldr_constants(self):
        """ extract the constant of the linear decision rule
            returns a matrix of constants with shape of x_shape X num_adaption_steps X num_adaption_elements
        """
        pi = self.x.get(self.z)
        pi[np.isnan(pi)] = 0
        return pi

    def get_x_values(self, sample=None):
        if sample is None:
            sample = np.zeros(self.z.shape)
        pi0 = self.x.get()
        pi = self.get_ldr_constants()

        pi = np.multiply(pi, sample)
        for _ in range(len(pi.shape) - len(pi0.shape)):
            pi = pi.sum(axis=-1)

        return pi0 + pi

    def retrieve_decision_vars(self, sample=None):
        if sample is None:
            sample = np.zeros(self.z.shape)

        return self.x(self.z.assign(sample))

    def get_results(self, sample=None):
        x = self.retrieve_decision_vars(sample)
        for name in self.sim.vars['name'].unique():
            xmin_idx, xmax_idx = self.get_x_idx(name)
            self.sim.network[name].vars['value'] = x[xmin_idx: xmax_idx]

        self.sim.vars['value'] = x
        self.tanks_balance()

    def tanks_balance(self):
        for t_name, t in self.sim.network.tanks.items():
            df = pd.DataFrame(index=self.sim.time_range, data={'df': 0})
            for x in t.pumps_inflows + t.cv_inflows:
                qin = x.vars['flow'] * x.vars['value']
                qin = qin.groupby(level='time').sum()
                df = pd.merge(df, qin.rename(x.name), left_index=True, right_index=True)

            for x in t.vsp_inflows + t.v_inflows:
                qin = x.vars['value']
                df = pd.merge(df, qin.rename(x.name), left_index=True, right_index=True)

            for x in t.pumps_outflows + t.cv_outflows:
                qout = x.vars['flow'] * x.vars['value']
                qout = -1 * qout.groupby(level='time').sum()
                df = pd.merge(df, qout.rename(x.name), left_index=True, right_index=True)

            for x in t.vsp_outflows + t.v_outflows:
                qout = -1 * x.vars['value']
                df = pd.merge(df, qout.rename(x.name), left_index=True, right_index=True)

            df['inflow'] = df.sum(axis=1)
            df['demand'] = -t.vars['demand']
            df['volume'] = t.initial_vol + (df['inflow'] + df['demand']).cumsum()
            t.vars['value'] = df['volume']
            t.vars['inflow'] = df['inflow']

    def multivariate_normal(self, n_sim):
        rng = np.random.default_rng()
        sigma = self.udata['demand'].sigma
        z = rng.multivariate_normal(np.zeros(sigma.shape[0]), sigma, size=n_sim, method='cholesky').T
        return z

    def set_vars_by_sample(self, single_sample):
        self.get_results(single_sample)

    def get_objective_by_sample(self):
        return (self.sim.vars['cost'] * self.sim.vars['value']).sum()

    def tank_vol_by_sample(self, tank_name, sample):
        tank = self.sim.network[tank_name]
        tank_vol = np.empty(sample.shape)
        dem_sample = self.nominal_demands + sample
        for j in range(dem_sample.shape[1]):
            s = sample[:, j].reshape(-1, 1)
            d = dem_sample[:, j].reshape(-1, 1)
            self.set_vars_by_sample(s)
            tank_inflow = self.sim.network.tanks[tank_name].vars['inflow'].values.reshape(-1, 1)
            tank_vol[:, j] = (tank.initial_vol + (tank_inflow - d).cumsum(axis=0)).T

        return tank_vol

    def analyze_sample(self, sample):
        dem_sample = self.nominal_demands + sample

        costs = []
        tanks_vol = {t_name: np.empty(sample.shape) for t_name, t in self.sim.network.tanks.items()}
        for j in range(sample.shape[1]):
            single_sample = sample[:, j].reshape(-1, 1)
            single_dem_sample = dem_sample[:, j].reshape(-1, 1)
            self.set_vars_by_sample(single_sample)

            costs.append(self.get_objective_by_sample())

            for tank_name, tank in self.sim.network.tanks.items():
                tank_inflow = self.sim.network.tanks[tank_name].vars['inflow'].values.reshape(-1, 1)
                tanks_vol[tank_name][:, j] = (tank.initial_vol + (tank_inflow - single_dem_sample).cumsum(axis=0)).T

        return costs, tanks_vol
