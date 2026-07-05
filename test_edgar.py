import os
import sys

import pandas as pd
from edgar import Company, set_identity

identity = (os.environ.get("SEC_EDGAR_IDENTITY") or os.environ.get("SCREENER_CONTACT_EMAIL", "")).strip()
if not identity:
    print("Set SEC_EDGAR_IDENTITY in .env (see .env.example)", file=sys.stderr)
    sys.exit(1)
set_identity(identity)

# 2. Initialize the target company (Apple Inc. - AAPL)
company = Company("AAPL")
facts = company.get_facts()

def get_historical_metric(concept_name):
    """Helper function to extract clean multi-year annual data for a GAAP concept"""
    try:
        # 1. Try exact match first (essential in newer edgartools versions to avoid fuzzy matches)
        df = facts.query().by_concept(concept_name, exact=True).to_dataframe()
        
        # Try with 'us-gaap:' prefix if not present and empty
        if df.empty and not concept_name.startswith("us-gaap:"):
            df = facts.query().by_concept(f"us-gaap:{concept_name}", exact=True).to_dataframe()
            
        # 2. Fallback to fuzzy match if exact match is empty
        if df.empty:
            clean_name = concept_name.replace("us-gaap:", "")
            df = facts.query().by_concept(clean_name).to_dataframe()
            
        if df.empty:
            return pd.Series(dtype=float)
            
        # Handle column names dynamically (handles both old and new edgartools versions)
        form_col = 'form_type' if 'form_type' in df.columns else 'form'
        fy_col = 'fiscal_year' if 'fiscal_year' in df.columns else 'fy'
        val_col = 'value' if 'value' in df.columns else ('numeric_value' if 'numeric_value' in df.columns else 'val')
        period_col = 'fiscal_period' if 'fiscal_period' in df.columns else 'fp'
        
        # Filter for 10-K or 10-K/A (annual filings)
        df_annual = df[df[form_col].isin(['10-K', '10-K/A'])].copy()
        if df_annual.empty:
            df_annual = df.copy()
            
        # Filter for full fiscal year period
        if period_col in df_annual.columns:
            df_fy = df_annual[df_annual[period_col] == 'FY']
            if not df_fy.empty:
                df_annual = df_fy
                
        if df_annual.empty:
            return pd.Series(dtype=float)
            
        # Sort and keep the last recorded value for each fiscal year to eliminate duplicates (keeps 10-K/A overrides)
        df_annual = df_annual.sort_values(by=[fy_col, 'filing_date' if 'filing_date' in df_annual.columns else fy_col])
        df_annual = df_annual.drop_duplicates(subset=[fy_col], keep="last")
        return df_annual.set_index(fy_col)[val_col]
    except Exception as e:
        return pd.Series(dtype=float)


def get_historical_metric_with_fallbacks(concept_names):
    """Try to get a metric using a list of alternative concept names, returning the first non-empty Series"""
    if isinstance(concept_names, str):
        concept_names = [concept_names]
    for name in concept_names:
        series = get_historical_metric(name)
        if not series.empty:
            return series
    return pd.Series(dtype=float)

# =====================================================================
# TASK 1: Share Count Trends & YoY Change
# =====================================================================
shares = get_historical_metric_with_fallbacks([
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic"
])

shares_df = pd.DataFrame({"Shares Outstanding": shares})
shares_df["Absolute Change"] = shares_df["Shares Outstanding"].diff()
shares_df["YoY % Change"] = shares_df["Shares Outstanding"].pct_change() * 100

print("--- SHARE COUNT HISTORICAL TRENDS ---")
print(shares_df.tail(10)) # View the most recent 10 years
print("\n")

# =====================================================================
# TASK 2: Multi-Year Annual ROIC Calculation
# =====================================================================
ebit = get_historical_metric_with_fallbacks(["OperatingIncomeLoss", "OperatingProfitLoss"])
ebt = get_historical_metric_with_fallbacks([
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    "IncomeBeforeIncomeTaxExpenseBenefit"
])
tax_expense = get_historical_metric_with_fallbacks(["IncomeTaxExpenseBenefit", "IncomeTaxExpenseBenefitContinuingOperations"])
equity = get_historical_metric_with_fallbacks(["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"])
cash = get_historical_metric_with_fallbacks([
    "CashAndCashEquivalentsAtCarryingValue",
    "CashAndCashEquivalentsAtCarryingValueWithFinancialInstitutions",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"
])

# Debt structures can vary by company; we pull short-term borrowings, other short-term debt, CP, and long-term debt
st_debt = get_historical_metric("ShortTermBorrowings")
other_st_debt = get_historical_metric("OtherShortTermBorrowings")
cp = get_historical_metric("CommercialPaper")
lt_debt_curr = get_historical_metric("LongTermDebtCurrent")
lt_debt_noncurr = get_historical_metric("LongTermDebtNoncurrent")

# Sum all available debt parts by aligning by index (fiscal year)
total_debt = pd.Series(dtype=float)
for s in [st_debt, other_st_debt, cp, lt_debt_curr, lt_debt_noncurr]:
    if not s.empty:
        total_debt = total_debt.add(s, fill_value=0)

# Fallback to total LongTermDebt if current/noncurrent split is empty
if total_debt.empty or total_debt.sum() == 0:
    total_debt = get_historical_metric("LongTermDebt")

# Combine into an analytical workbench
roic_df = pd.DataFrame({
    "EBIT": ebit,
    "EBT": ebt,
    "Tax_Expense": tax_expense,
    "Total_Debt": total_debt,
    "Equity": equity,
    "Cash": cash
}).dropna() # Keep years where all parameters are available

# Step A: Compute Effective Tax Rate & NOPAT (Net Operating Profit After Tax)
roic_df["Effective_Tax_Rate"] = roic_df["Tax_Expense"] / roic_df["EBT"]
roic_df["NOPAT"] = roic_df["EBIT"] * (1 - roic_df["Effective_Tax_Rate"])

# Step B: Compute Invested Capital
# Formula: Total Debt + Equity - Cash
roic_df["Invested_Capital"] = roic_df["Total_Debt"] + roic_df["Equity"] - roic_df["Cash"]

# Step C: Compute ROIC %
roic_df["ROIC (%)"] = (roic_df["NOPAT"] / roic_df["Invested_Capital"]) * 100

print("--- HISTORICAL ROIC BREAKDOWN ---")
pd.set_option('display.max_columns', None)
pd.set_option('display.width', 1000)
print(roic_df[["EBIT", "NOPAT", "Invested_Capital", "ROIC (%)"]].tail(10))