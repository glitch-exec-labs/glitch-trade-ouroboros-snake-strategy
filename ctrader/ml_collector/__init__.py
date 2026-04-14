"""
Glitch ML Data Collector — clean-slate research pipeline.

Runs momentum + mean-reversion strategies on a fresh cTrader demo account,
logs every signal + trade + outcome to daily CSV files, and pushes those
CSVs to the glitch-executor-ml-data GitHub repo under ml_data_clean/.

Hard-isolated from the production trading stack: dedicated user, separate
venv, separate .env, never touches the live account (46868136).
"""
__version__ = "0.1.0"
