"""
Glitch ML Data Collector — clean-slate research pipeline.

Runs the six-snake ensemble on cTrader demo accounts, logs every
signal + trade + outcome to daily CSV files, and pushes those CSVs to
the companion ml-data GitHub repo under ml_data_clean/.

Hard-isolated from the production trading stack: dedicated user, separate
venv, separate .env, refuses to start on the ML_FORBIDDEN_ACCOUNT_ID
configured in the runtime .env.
"""
__version__ = "0.1.0"
