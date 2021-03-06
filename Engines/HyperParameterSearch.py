import sys
import warnings
import gurobipy as gp
from gurobipy import *
import pandas as pd
import numpy as np
import tqdm
from PeakToTroughAndUnderWater import PeakToTrough, UnderWater
warnings.filterwarnings("ignore")
from os.path import dirname, join

project_root = dirname(dirname(__file__))
output_path = join(project_root, 'Result', "")

"""

Hyperparameter to search: kappa, look_back period, rebalancing frequency


"""


class AnomalyOpt:
    def __init__(self, Data, Rho):
        self.Data = Data
        self.asset_names = self.Data.columns # remember to drop the column ['DATE']
        self.num_assets = len(self.Data.columns)
        self.kappas = [0.001, 0.01, 0.1, 1, 10, 100, 1000]
        self.look_backs = [5, 21, 42, 63, 125, 189, 250, 500] #[7, 30, 90, 180] #number of days to look back
        self.rebalancing_frequency = [5, 21]
        self.P = 250 # monthly frequency
        self.Rho = Rho # target annualized
        self.rho = self.Rho / self.P
        self.lag = 1

    def solve(self, data, w_prev, rho, kappa):
        """
        Perform a single iteration of optimization
        :param data: the matrix where row=date and column = asset names
        :param w_prev: the previously weight vector, if not available then set to []
        :param rho: the normalized return
        :param kappa: cost of trading
        :return: the optimized w
        """
        mu = data.mean(axis=0)  # n by 1
        var = data.var(axis=0, ddof=1)

        # Create a new model
        m = gp.Model("portfolio")
        # Add a variable for each stock
        w = pd.Series(m.addVars(self.asset_names, lb=-0.1, ub=0.1), index=self.asset_names)
        aux_vars = m.addVars(range(self.num_assets))
        if  w_prev == []:
            shifting = 0
        else:
            shifting = kappa * quicksum([(w_prev[i] - w[i]) ** 2 for i in range(self.num_assets)])

        # Objective is to minimize risk (squared).  This is modeled using the
        # covariance matrix, which measures
        # the historical correlation between stocks.
        regularization = quicksum([w[i] ** 2 * var[i] for i in range(self.num_assets)])  # exclude risk-free cashflow
        portfolio_risk = ((data.dot(w) - rho) ** 2).sum() + regularization + shifting
        m.setObjective(portfolio_risk, GRB.MINIMIZE)

        # Fix budget with a constraint
        #m.addConstr(w.sum() == 1, 'budget')
        m.addConstr(mu.dot(w) == rho, "target return")
        m.addConstr(aux_vars.sum() <= 2, "gross exposure")

        m.addConstrs((aux_vars[i] == abs_(w[i]) for i in range(self.num_assets)))
        # Optimize model to find the minimum risk portfolio
        m.setParam('OutputFlag', 0)
        m.setParam('TimeLimit', 5*60)# allow only 1 minute for each optimization problem
        m.optimize()
        try:
            sol = [v.x for v in m.getVars()]
            w = np.array(sol[:self.num_assets]).reshape((-1, 1))
            leverage = np.sum(sol[self.num_assets:])
            try:
                assert leverage <= 1
                assert np.isclose(mu.dot(w),rho).all()
            except AssertionError as e:
                print(leverage, mu.dot(w)[0], rho)
        except AttributeError as e:
            w = np.zeros(self.num_assets).reshape((-1, 1))
            print("Infeasible !")

        # r = R_train.dot(w).values
        return w

    def preProcessing(self, data):
        """
        Scale each column to have the same annualized std. but the scaled factor is capped by 2.
        :param data: the training data 
        :return: Normalized data
        """
        normalization_factors = data.apply(lambda x: min(0.15 / np.sqrt(self.P) / x.std(), 2), axis=0)
        data = data.apply(lambda x: min(0.15 / np.sqrt(self.P) / x.std(), 2) * x, axis=0)
        return data, normalization_factors

    def postProcessing(self):
        return

    def gridSearch(self):
        performance_dict = {} # record the performance of each combination of hyper-parameter
        grid = [(kappa, look_back, freq) for kappa in self.kappas for look_back in self.look_backs for freq in self.rebalancing_frequency]
        for grid_point in tqdm.tqdm(grid, desc="Hyper-Parameter Searching", position=0, leave=True):
            kappa, look_back, freq = grid_point
            num_periods = self.Data.shape[0]
            W = []  # container of w's
            sigma_factor = [] # container of normalization factors 
            test_start = look_back  # we start validation once enough data is available

            for test_period in range(test_start, num_periods, freq):
                R_train = self.Data.iloc[max(0, test_period - look_back):test_period, :]
                R_train_normalized, normalization_factors = self.preProcessing(R_train)

                w_old = W[-1] if len(W) != 0 else []
                save_stdout = sys.stdout
                sys.stdout = open('trash', 'w')
                w = self.solve(R_train_normalized, w_old, self.rho, kappa)
                sys.stdout = save_stdout
                for _ in range(freq):
                    if len(W) < num_periods - test_start:
                        W.append(w.flatten())
                        sigma_factor.append(normalization_factors.to_numpy())
                    else:
                        break
            W = np.array(W)
            sigma_factor = np.array(sigma_factor)
            R_test = self.Data.iloc[test_start:, :]

            r = np.sum(R_test[self.lag:] * sigma_factor[:-self.lag] * W[:-self.lag], axis=1)  # monthly return vector
            #score = self.score(r)
            performance_dict[grid_point] = r.values
        return performance_dict

    def metrics(self, W, r):
        max_drop_down = PeakToTrough(r)
        max_time_under_water = UnderWater(r)
        return max_drop_down, max_time_under_water

    def summaryResult(self):
        performance = self.gridSearch()# key is combination of hyper-parameter, value is the return vector
        result_df = pd.Series(performance, name="r").to_frame() # row index is is combination of hyper-parameter, one column named r cotains the return vector
        result_df.to_csv("Return vector.csv")
        result_df["annual return"] = result_df['r'].apply(lambda x: float(self.P * np.mean(x)))
        result_df["annual risk"]   = result_df['r'].apply(lambda x: float(np.sqrt(self.P) * np.std(x, ddof=0)))
        result_df["sharpe ratio"]  = result_df["annual return"] / result_df["annual risk"]
        result_df['max_drop_down'] = result_df['r'].apply(PeakToTrough)
        result_df['max_time_under_water '] = result_df['r'].apply(UnderWater)
        #result_df.round(2).to_csv("C:\\Users\\apply\\Desktop\\JYH\\Quant Research\\Porfolio Optimization\\Optimizer\\Result\\normalization_column_3529-4779_with lag_adjusted.csv")
        result_df.round(2).to_csv("C:\\Users\\apply\\Desktop\\JYH\\OneDrive\\Ghost\\Pending\\stock.csv")



    def equalWeight(self):
        w = 1/self.num_assets * np.ones((self.num_assets, 1))
        r = self.Data.dot(w)
        print(float(self.P * np.mean(r)))
        print(float(np.sqrt(self.P) * np.std(r, ddof=0)))

if __name__ == "__main__":
    data = pd.read_csv(join(project_root, "Data/return_2000_2018.csv"), index_col="DATE")
    data = data.loc[:, data.columns != 'DATE'].fillna("0%").applymap(lambda x: float(x.split("%")[0]) / 100)
    #data = pd.read_csv(join(project_root, "Data/ret/ret_transformed.csv"), index_col="DATE")
    #data=data.loc[:, data.columns != 'DATE'].fillna(0).astype(float)#.applymap(lambda x: float(x.split("%")[0]) / 100)

    data.to_csv("cleanData.csv")
    test = AnomalyOpt(data.iloc[3529:], 0.3) # use 2000-2010 to hyper-search 0:2515
    test.summaryResult()