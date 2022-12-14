# adapting methods from
# https://towardsdatascience.com/transformer-unleashed-deep-forecasting-of-multivariate-time-series-in-python-9ca729dac019
import pandas as pd
import numpy as np
import os
from sklearn.decomposition import PCA
from sklearn.preprocessing import MinMaxScaler
from darts import TimeSeries, concatenate
from darts.dataprocessing.transformers import Scaler
from darts.models.forecasting.xgboost import XGBModel
from darts.metrics import mape, rmse
from darts.utils.timeseries_generation import datetime_attribute_timeseries
from darts.utils.likelihood_models import QuantileRegression
from datetime import datetime

from utils import predQ, adf_test, invert_transformation

def run_xgboost_loop(result_log = None, pred_df = None, start_val= 0,
                         input_len_used = 12, period_past_data = None, targets_sample = None, min_month_avg = 50, min_tot_inc = 50
                         , ccc_taught_only = True, differenced = False, use_other_skill=True, run_name = ''):
    '''
    params:
        df - job posting counts dataframe
        targets - set of targets to run loop on
        result_log - previous result log data frame
        pred_df - previous prediction results dataframe
        start_val - skill number to start at for interrupted runs
        input_len_used - how many months prior to train on
        period_past_data - how many time periods (months) worth of data to use. If None, use all data provided.
        targets_sample - length of subset of targets to train on; used for shortening runtimes of tests
        min_month_avg - minimum monthly average job postings for skill to be forecasted for
        min_tot_inc - minimum total increase between first and last observed month for skill to be forecasted for
        use_other_skills - whether to use other skills to predict future values
        run_name - name to give run's log/results files.

    Function to test run xgboost model with various parameters, and understand runtime
    '''

    date_run = datetime.now().strftime('%H_%M_%d_%m_%Y')
    SPLIT = 0.9         # train/test %

    if result_log is None:
        result_log = pd.DataFrame()

    df = pd.read_csv('data/test monthly counts season-adj.csv', index_col=0)

    #--------------------
    # Feature Selection
    #-------------------

    # look only for those skills with mean 50 postings, or whose postings count have increased by 50 from the first to last month monitored

    raw_df = pd.read_csv('data/test monthly counts.csv')
    raw_df = raw_df.rename({'Unnamed: 0': 'date'}, axis=1)
    raw_df = raw_df.fillna(method='ffill')
    # 7-55 filter is to remove months with 0 obs
    raw_df = raw_df.iloc[7:55, :].reset_index(drop=True)
    # normalize all columns based on job postings counts
    raw_df = raw_df.drop('date', axis=1)

    # identify those skills who have from first to last month by at least 50 postings
    demand_diff = raw_df.T.iloc[:, -1] - raw_df.T.iloc[:, 0]
    targets = raw_df.mean(numeric_only=True).loc[(raw_df.mean(numeric_only=True)>min_month_avg)|(demand_diff > min_tot_inc)].index

    date_idx = pd.to_datetime(df.index)
    df = df.set_index(pd.DatetimeIndex(date_idx))

    # include on CCC-taught skills
    if ccc_taught_only:
        ccc_df = pd.read_excel('emsi_skills_api/course_skill_counts.xlsx')
        ccc_df.columns = ['skill', 'count']
        ccc_skills = ['Skill: ' + i for i in ccc_df['skill']]
        targets = set(ccc_skills).intersection(set(targets)).union(set(['Postings count']))
        targets = list(targets)
        targets.sort()

    #targets = df.columns[1:]
    targets = targets[start_val:]
    if targets_sample is not None:
        targets = targets[:targets_sample]

    # add in COVID case count data
    covid_df = pd.read_csv('data/NYT COVID us-counties clean.csv')
    # add 0 rows for pre-covid years
    for y in range(2018,2020):
        for m in range(1,13):
            covid_df = covid_df.append(pd.Series([y, m, 0], index= ['year','month','cases_change']), ignore_index = True)

    # reshape to match the features data set and merge with features data
    covid_df = covid_df.sort_values(['year','month'])
    covid_df = covid_df.iloc[7:,:]
    covid_df.index = date_idx
    covid_df = covid_df.drop(['year','month'],axis=1)
    covid_df.columns = ['covid_cases']
    targets.append(pd.Index(['covid_cases']))

    df = df.merge(covid_df, left_index = True, right_index = True)

    orig_df = df.copy()

    # ------------------------
    # Model Execution
    #------------------------

    # set a variable to target
    print('Number of targets:',len(targets))
    if pred_df is None:
        pred_df = pd.DataFrame()
    features_main = df.corr()
    orig_df = df.copy()
    for n,t in enumerate(targets):
        df = orig_df
        # only forecast skills
        if 'Skill' not in t:
            continue
        # if no postings exist, skip skill
        if df[t].sum() == 0:
            continue
        start = datetime.now()
        print('Modeling',n,'of',len(targets),'skills')

        # option to perform differencing on non-stationary skills
        diffs_made = 0
        if differenced:
            # check to see if any of the series are non-stationary

            if df[t].sum() != 0:
                result = adf_test(df[t])
                if result > .05:
                    diffs_made += 1
                    df = df.diff().dropna()

                    # rerun stationary tests on results
                    result2 = adf_test(df[t])

                    # if still non-stationary, difference again
                    if result2 > .05:
                        diffs_made += 1
                        df = df.diff().dropna()


        # shorten df to only have time steps of length period_past_data
        # if not specified, use all data
        if period_past_data == None:
            period_past_data = df.shape[0]
        df = df.iloc[-period_past_data:]

        if use_other_skill:
            # figure out what features to use
            features = features_main[t]
            # filter to only those with at least a moderate correlation of .25
            features = features.loc[features.abs()> .25]
            features = features.drop(t).index

            # min max scale features
            df_feat = df[features]
        else:
            df_feat = df[[t]]
        df_feat = pd.DataFrame(MinMaxScaler().fit_transform(df_feat))
        df_feat.index = df.index
        # # run PCA to reduce number of features
        # pca = PCA(n_components=min(pca_components, len(features)))
        # res_pca = pca.fit_transform(df_feat)
        #
        # # collect principal components in a dataframe
        # df_pca = pd.DataFrame(res_pca)
        # df_pca.index = df.index
        # df_pca = df_pca.add_prefix("pca")
        # df_pca[t] = df[t]

        # select pcas with correlation >.10
        # removing this part as it was just done for ease of explanability
        # selected_pca = df_pca.corr()[t].loc[df_pca.corr()[t].abs() > .1].drop(t).index

        #df.corr().to_csv('data/test corr.csv')

        # convert target to time series
        ts_P = TimeSeries.from_series(df[t], fill_missing_dates=True, freq=None)
        #ts_P = pd.Series([i[0] for i in ts_P.values()])
        # convert features to time series
        ts_covF = TimeSeries.from_dataframe(df_feat, fill_missing_dates=True, freq=None)

        # create train and test split
        ts_train, ts_test = ts_P.split_after(SPLIT)
        covF_train, covF_test = ts_covF.split_after(SPLIT)

        scalerP = Scaler()
        scalerP.fit_transform(ts_train)
        ts_ttrain = scalerP.transform(ts_train)
        ts_ttest = scalerP.transform(ts_test)
        ts_t = scalerP.transform(ts_P)

        # make sure data are of type float
        ts_t = ts_t.astype(np.float32)
        ts_ttrain = ts_ttrain.astype(np.float32)
        ts_ttest = ts_ttest.astype(np.float32)

        # do the same for features
        scalerF = Scaler()
        scalerF.fit_transform(covF_train)
        covF_ttrain = scalerF.transform(covF_train)
        covF_ttest = scalerF.transform(covF_test)
        covF_t = scalerF.transform(ts_covF)


        # make sure data are of type float
        covF_ttrain = covF_ttrain.astype(np.float32)
        covF_ttest = covF_ttrain.astype(np.float32)
        covF_t = covF_t.astype(np.float32)

        # add monthly indicators
        covT = datetime_attribute_timeseries(ts_P.time_index,
                                                attribute="month",
                                                one_hot=False)


        # train/test split
        covT_train, covT_test = covT.split_after(SPLIT)

        # rescale the covariates: fitting on the training set
        scalerT = Scaler()
        scalerT.fit(covT_train)
        covT_ttrain = scalerT.transform(covT_train)
        covT_ttest = scalerT.transform(covT_test)
        covT_t = scalerT.transform(covT)

        covT_t = covT_t.astype(np.float32)


        count = 0
        output_chunk_len = len(covF_ttrain) - input_len_used
        while True:
            try:
                model = XGBModel(
                    lags=12,
                    lags_past_covariates=12,
                    output_chunk_length= output_chunk_len
                )
                model.fit(ts_ttrain,
                                past_covariates=covF_ttrain,
                                verbose=True)
                break
            except (FileNotFoundError, PermissionError, FileExistsError):
                if count < 20:
                    print('PermissionError, retrying')
                    import time
                    time.sleep(1)
                    count += 1
                    continue
                else:
                    raise('too many attempts at model training')

        #model.save('models/test model.pth.tar')

        ts_tpred_long = model.predict(   n=output_chunk_len,
                                    past_covariates=covF_t,
                                    verbose=True)
        # mark the test set for evaluation
        ts_tpred = ts_tpred_long[:len(ts_test)]

        # take the rest of the predictions and transform them back into a dataframe
        ts_tfut = ts_tpred_long[len(ts_test):]

        # remove the scaler transform
        ts_tfut = scalerP.inverse_transform(ts_tfut)

        # convert to dataframe
        pred_row = pd.DataFrame(ts_tfut.values())[0]

        # revert differencing if any differences made
        if diffs_made > 0:
            if diffs_made == 2:
                pred_row = (df[t].iloc[-1] - df[t].iloc[-2]) + pred_row.cumsum()
            pred_row = df[t].iloc[-1] + pred_row.cumsum()
        # concatenate to df
        if pred_df.empty:
            pred_df = pd.DataFrame(index = pred_row.index)
        else:
            pred_df.index = pred_row.index
        pred_df = pd.concat([pred_df, pred_row],axis=1)

        # retrieve forecast series for chosen quantiles,
        # inverse-transform each series,
        # insert them as columns in a new dataframe dfY
        q50_RMSE = np.inf
        q50_MAPE = np.inf
        ts_q50 = None
        pd.options.display.float_format = '{:,.2f}'.format
        dfY = pd.DataFrame()
        dfY["Actual"] = TimeSeries.pd_series(ts_test)

        # call helper function predQ
        perf_scores = predQ(ts_tpred, None, scalerP, dfY, ts_test, quantile=False)
        row = pd.Series()
        row['target'] = t
        row['Normalized RMSE'] = perf_scores[0]/(df[t].max() - df[t].min())
        row['MAPE']= perf_scores[1]
        row['runtime'] = datetime.now() - start
        row['num_features_used'] = len(df_feat.columns)

        result_log = result_log.append(row, ignore_index=True)

        # log results
        result_log['forecast method'] = 'xgboost'
        result_log['timestamp'] = date_run
        # result_log['RMSE'] = perf_scores[0]
        # result_log['MAPE'] = perf_scores[1]
        result_log['num_features_raw'] = df.shape[1] - 2
        if use_other_skill:
            result_log['num_features_used'] = len(features)
        else:
            result_log['num_features_used'] = 0
        # result_log['pca_components'] = pca_components
        result_log['RUN_NAME'] = run_name
        result_log['ccc_taught_only'] = ccc_taught_only
        result_log['input_len_used'] = input_len_used

        pd.DataFrame(result_log).T.to_csv('result_logs/looped transformer model results '+
                                          date_run+' '+run_name +
                                          '.csv')

        pred_df.to_csv('output/predicted job posting shares '+
                                          date_run+' '+run_name+
                                          '.csv')

# obsolete function
# def prepare_data():
#     '''
#     load data in preparation for running the transformer loop over each feature
#     DEPRECIATED - not needed, cleaning now don in remove_seasonality file.
#     '''
#
#     # df = pd.read_csv('data/test monthly counts 09302022.csv')
#     df = pd.read_csv('data/test monthly counts.csv')
#     df = df.rename({'Unnamed: 0':'date'}, axis=1)
#     df['month']= df['date'].str[5:7].astype('int')
#     df = df.fillna(method='ffill')
#     # 7-55 filter is to remove months with 0 obs
#     df = df.iloc[7:55,:].reset_index(drop=True)
#
#     # create times series index
#     # normalize all columns based on job postings counts
#     df = df.drop('date', axis=1)
#     job_counts = df['Postings count'].copy()
#     raw_df = df.copy()
#     df = df.divide(job_counts, axis=0)
#     df['Postings count'] = job_counts
#     date_idx = pd.to_datetime(df['date'])
#     df = df.set_index(pd.DatetimeIndex(date_idx))
#
#     # identify those skills who have from first to last month by at least 50 postings
#     demand_diff = raw_df.T.iloc[:, -1] - raw_df.T.iloc[:, 0]
#
#     # establish target columns as ones with an average obs count over 100
#     # targets = raw_df.mean(numeric_only=True).loc[raw_df.mean(numeric_only=True)>100].index
#     # trying going down to 50
#     targets = raw_df.mean(numeric_only=True).loc[(raw_df.mean(numeric_only=True)>50)|(demand_diff > 50)].index
#
#     # filter to only skills trained by CCC
#     ccc_df = pd.read_excel('emsi_skills_api/course_skill_counts.xlsx')
#     ccc_df.columns = ['skill', 'count']
#     ccc_skills = ['Skill: ' + i for i in ccc_df['skill']]
#     targets = set(ccc_skills).intersection(set(targets)).union(set(['Postings count']))
#     targets = list(targets)
#     targets.sort()
#     return df, targets
