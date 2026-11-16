"""52-Category taxonomy, Indian bank profiles, and the curated categorisation knowledge base."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Tuple

from cyphersol.models import VoucherType


@dataclass(frozen=True)
class CategorySpec:
    category_id: int
    group: str
    name: str
    typical_direction: str  # CR, DR, BOTH
    tally_voucher: VoucherType
    tally_parent_group: str
    is_lifestyle: bool = False
    is_cash_crypto_leakage: bool = False
    itr_section: str = ""


CATEGORIES_52: List[CategorySpec] = [
    # 01-10: Income & Receipts
    CategorySpec(1, "Income & Receipts", "Salary & Wages Received", "CR", VoucherType.RECEIPT, "Direct Incomes", itr_section="Salary (Sec 15-17)"),
    CategorySpec(2, "Income & Receipts", "Business Sales & Trade Receipts", "CR", VoucherType.RECEIPT, "Sales Accounts", itr_section="Business Turnover (PGBP)"),
    CategorySpec(3, "Income & Receipts", "Professional & Consultancy Fees", "CR", VoucherType.RECEIPT, "Direct Incomes", itr_section="Sec 44ADA / PGBP"),
    CategorySpec(4, "Income & Receipts", "Interest Income (Savings / FD / Sweep)", "CR", VoucherType.RECEIPT, "Indirect Incomes", itr_section="Sec 80TTA / 80TTB / OS"),
    CategorySpec(5, "Income & Receipts", "Dividend & Capital Gain Payouts", "CR", VoucherType.RECEIPT, "Indirect Incomes", itr_section="Schedule OS / CG"),
    CategorySpec(6, "Income & Receipts", "Rental & Lease Income", "CR", VoucherType.RECEIPT, "Indirect Incomes", itr_section="House Property (Sec 22)"),
    CategorySpec(7, "Income & Receipts", "Tax Refund (Income Tax / GST)", "CR", VoucherType.RECEIPT, "Duties & Taxes", itr_section="Refund Verification"),
    CategorySpec(8, "Income & Receipts", "Loan / Credit Line Disbursal Received", "CR", VoucherType.RECEIPT, "Loans (Liability)", itr_section="Non-Taxable Capital Receipt"),
    CategorySpec(9, "Income & Receipts", "Capital / Partner / Director Infusion", "CR", VoucherType.RECEIPT, "Capital Account", itr_section="Sec 68 Source Verification"),
    CategorySpec(10, "Income & Receipts", "Commission, Brokerage & Incentive Income", "CR", VoucherType.RECEIPT, "Indirect Incomes", itr_section="TDS Sec 194H"),

    # 11-22: Operating & Business Expenses
    CategorySpec(11, "Operating & Business", "Vendor & Supplier Payments", "DR", VoucherType.PAYMENT, "Sundry Creditors", itr_section="Sec 194C / PGBP"),
    CategorySpec(12, "Operating & Business", "Inventory & Raw Material Purchase", "DR", VoucherType.PAYMENT, "Purchase Accounts", itr_section="COGS / PGBP"),
    CategorySpec(13, "Operating & Business", "Office / Commercial Rent & Maintenance", "DR", VoucherType.PAYMENT, "Indirect Expenses", itr_section="TDS Sec 194I"),
    CategorySpec(14, "Operating & Business", "Employee Payroll, Wages & Stipends", "DR", VoucherType.PAYMENT, "Indirect Expenses", itr_section="Sec 192 Payroll"),
    CategorySpec(15, "Operating & Business", "Electricity, Water & Gas Utilities", "DR", VoucherType.PAYMENT, "Indirect Expenses"),
    CategorySpec(16, "Operating & Business", "Telecom, Broadband & Mobile Bills", "DR", VoucherType.PAYMENT, "Indirect Expenses"),
    CategorySpec(17, "Operating & Business", "Software, SaaS & Cloud Subscriptions", "DR", VoucherType.PAYMENT, "Indirect Expenses"),
    CategorySpec(18, "Operating & Business", "Legal, Audit & Professional Consultancy Paid", "DR", VoucherType.PAYMENT, "Indirect Expenses", itr_section="TDS Sec 194J"),
    CategorySpec(19, "Operating & Business", "Marketing, Digital Ads & Promotion", "DR", VoucherType.PAYMENT, "Indirect Expenses"),
    CategorySpec(20, "Operating & Business", "Freight, Logistics, Transport & Courier", "DR", VoucherType.PAYMENT, "Direct Expenses"),
    CategorySpec(21, "Operating & Business", "Printing, Stationery & Office Supplies", "DR", VoucherType.PAYMENT, "Indirect Expenses"),
    CategorySpec(22, "Operating & Business", "Repairs, AMC & Facility Maintenance", "DR", VoucherType.PAYMENT, "Indirect Expenses"),

    # 23-30: Banking, Finance & EMIs
    CategorySpec(23, "Banking & Debt", "Home Loan / Mortgage EMI", "DR", VoucherType.PAYMENT, "Secured Loans", itr_section="Sec 24(b) & Sec 80C"),
    CategorySpec(24, "Banking & Debt", "Vehicle / Auto Loan EMI", "DR", VoucherType.PAYMENT, "Secured Loans"),
    CategorySpec(25, "Banking & Debt", "Personal / Business / Unsecured Loan EMI", "DR", VoucherType.PAYMENT, "Unsecured Loans"),
    CategorySpec(26, "Banking & Debt", "Credit Card Bill Settlement", "DR", VoucherType.PAYMENT, "Current Liabilities", itr_section="SFT Credit Card Check"),
    CategorySpec(27, "Banking & Debt", "Bank Charges, SMS & Min Balance Penalties", "DR", VoucherType.PAYMENT, "Indirect Expenses"),
    CategorySpec(28, "Banking & Debt", "Cheque / ECS / NACH Bounce & Return Charges", "DR", VoucherType.PAYMENT, "Indirect Expenses"),
    CategorySpec(29, "Banking & Debt", "OD / CC / Loan Interest & Processing Fees", "DR", VoucherType.PAYMENT, "Indirect Expenses"),
    CategorySpec(30, "Banking & Debt", "Forex, Remittance & LRS Charges", "BOTH", VoucherType.PAYMENT, "Indirect Expenses", itr_section="FEMA / LRS TCS"),

    # 31-37: Investments & Wealth
    CategorySpec(31, "Investments & Wealth", "Mutual Fund / SIP Investment", "DR", VoucherType.PAYMENT, "Investments", itr_section="Sec 80C (ELSS)"),
    CategorySpec(32, "Investments & Wealth", "Equity / Stock Broker Settlement (Demat)", "BOTH", VoucherType.PAYMENT, "Investments", itr_section="Schedule CG / Trading"),
    CategorySpec(33, "Investments & Wealth", "Fixed Deposit / RD Creation (Sweep Out)", "DR", VoucherType.PAYMENT, "Deposits (Asset)"),
    CategorySpec(34, "Investments & Wealth", "Fixed Deposit / Investment Maturity Proceeds", "CR", VoucherType.RECEIPT, "Deposits (Asset)"),
    CategorySpec(35, "Investments & Wealth", "Insurance Premium (Life / Health / Motor)", "DR", VoucherType.PAYMENT, "Indirect Expenses", itr_section="Sec 80C / 80D"),
    CategorySpec(36, "Investments & Wealth", "Provident Fund / Retirement (EPF / PPF / NPS)", "DR", VoucherType.PAYMENT, "Investments", itr_section="Sec 80C / 80CCD"),
    CategorySpec(37, "Investments & Wealth", "Crypto / Virtual Digital Asset (VDA) Exchange", "BOTH", VoucherType.PAYMENT, "Investments", is_cash_crypto_leakage=True, itr_section="Sec 115BBH VDA"),

    # 38-42: Taxes & Statutory Dues
    CategorySpec(38, "Taxes & Statutory", "Income Tax / Advance Tax / TDS Challan", "DR", VoucherType.PAYMENT, "Duties & Taxes", itr_section="Advance Tax / TDS"),
    CategorySpec(39, "Taxes & Statutory", "GST Payment (PMT-06 / Challan)", "DR", VoucherType.PAYMENT, "Duties & Taxes", itr_section="GST Compliance"),
    CategorySpec(40, "Taxes & Statutory", "Professional Tax / Municipal / Property Tax", "DR", VoucherType.PAYMENT, "Duties & Taxes"),
    CategorySpec(41, "Taxes & Statutory", "Customs Duty & Import/Export Levies", "DR", VoucherType.PAYMENT, "Duties & Taxes"),
    CategorySpec(42, "Taxes & Statutory", "Government Fees, Stamp Duty & Penalties", "DR", VoucherType.PAYMENT, "Indirect Expenses"),

    # 43-48: Personal & Lifestyle Spending
    CategorySpec(43, "Personal & Lifestyle", "Food Delivery, Dining & Restaurants", "DR", VoucherType.PAYMENT, "Capital Account", is_lifestyle=True),
    CategorySpec(44, "Personal & Lifestyle", "Groceries, Kirana & Supermarkets", "DR", VoucherType.PAYMENT, "Capital Account", is_lifestyle=True),
    CategorySpec(45, "Personal & Lifestyle", "Fuel, EV Charging, Fastag & Local Commute", "DR", VoucherType.PAYMENT, "Capital Account", is_lifestyle=True),
    CategorySpec(46, "Personal & Lifestyle", "Travel, Airlines, Railways & Hotels", "DR", VoucherType.PAYMENT, "Capital Account", is_lifestyle=True),
    CategorySpec(47, "Personal & Lifestyle", "Healthcare, Hospitals & Pharmacy", "DR", VoucherType.PAYMENT, "Capital Account", is_lifestyle=True, itr_section="Sec 80D Medical"),
    CategorySpec(48, "Personal & Lifestyle", "E-Commerce, Apparel & Retail Shopping", "DR", VoucherType.PAYMENT, "Capital Account", is_lifestyle=True),

    # 49-52: Cash, Transfers & Suspense
    CategorySpec(49, "Cash, Transfers & Suspense", "Self / Inter-Account Contra Transfer", "BOTH", VoucherType.CONTRA, "Bank Accounts"),
    CategorySpec(50, "Cash, Transfers & Suspense", "Cash Deposit (Branch / CDM)", "CR", VoucherType.CONTRA, "Cash-in-Hand", itr_section="SFT Cash > 10L"),
    CategorySpec(51, "Cash, Transfers & Suspense", "Cash Withdrawal (ATM / AEPS / Bearer Chq)", "DR", VoucherType.CONTRA, "Cash-in-Hand", is_cash_crypto_leakage=True, itr_section="Sec 40A(3) Check"),
    CategorySpec(52, "Cash, Transfers & Suspense", "Suspense / Uncategorized", "BOTH", VoucherType.JOURNAL, "Suspense A/c"),
]

CATEGORY_BY_ID: Dict[int, CategorySpec] = {c.category_id: c for c in CATEGORIES_52}
CATEGORY_BY_NAME: Dict[str, CategorySpec] = {c.name.lower(): c for c in CATEGORIES_52}

# The 28 groups that ship pre-defined in every Tally Prime / ERP 9 company. Ledgers created under any
# other parent fail on import unless a <GROUP> master is emitted first, so every category maps here.
TALLY_PREDEFINED_GROUPS: FrozenSet[str] = frozenset(
    {
        "Bank Accounts", "Bank OD A/c", "Branch / Divisions", "Capital Account", "Cash-in-Hand",
        "Current Assets", "Current Liabilities", "Deposits (Asset)", "Direct Expenses", "Direct Incomes",
        "Duties & Taxes", "Fixed Assets", "Indirect Expenses", "Indirect Incomes", "Investments",
        "Loans & Advances (Asset)", "Loans (Liability)", "Misc. Expenses (ASSET)", "Provisions",
        "Purchase Accounts", "Reserves & Surplus", "Sales Accounts", "Secured Loans", "Stock-in-Hand",
        "Sundry Creditors", "Sundry Debtors", "Suspense A/c", "Unsecured Loans",
    }
)

# Ledger (not group) used for uncategorised rows. It must differ from the predefined "Suspense A/c"
# group name, otherwise Tally rejects the ledger master as a name clash.
SUSPENSE_LEDGER_NAME = "Suspense Entries"
SUSPENSE_CATEGORY_ID = 52

# Categories that are genuine contra movements (bank <-> cash / bank <-> own bank). Everything else is a
# Payment (money out) or Receipt (money in) voucher decided by the transaction direction.
CONTRA_CATEGORY_IDS: FrozenSet[int] = frozenset({49, 50, 51})


def tally_voucher_for(category_id: int, direction: str) -> VoucherType:
    """Voucher type for a category given the row direction ("CR"/"DR").

    Contra categories stay Contra; Sales/Purchase specs are kept; all others (including Suspense and the
    BOTH-direction categories) become Receipt for credits and Payment for debits.
    """
    spec = CATEGORY_BY_ID.get(category_id, CATEGORY_BY_ID[SUSPENSE_CATEGORY_ID])
    if spec.category_id in CONTRA_CATEGORY_IDS:
        return VoucherType.CONTRA
    if spec.tally_voucher in (VoucherType.SALES, VoucherType.PURCHASE):
        return spec.tally_voucher
    return VoucherType.RECEIPT if str(direction).upper() == "CR" else VoucherType.PAYMENT


def category_allows_direction(category_id: int, direction: str) -> bool:
    spec = CATEGORY_BY_ID.get(category_id)
    return bool(spec) and spec.typical_direction in ("BOTH", str(direction).upper())


def _validate_taxonomy() -> None:
    bad = [c.category_id for c in CATEGORIES_52 if c.tally_parent_group not in TALLY_PREDEFINED_GROUPS]
    if bad:  # pragma: no cover - guarded by tests; fail fast on a bad edit
        raise ValueError(f"Categories with non-Tally parent groups: {bad}")


_validate_taxonomy()


@dataclass(frozen=True)
class BankProfile:
    bank_code: str
    bank_name: str
    ifsc_prefix: str
    keywords: Tuple[str, ...]


# 105+ Scheduled, Public, Private, Foreign, Small Finance, Payments & Cooperative Indian Banks
INDIAN_BANKS: List[BankProfile] = [
    BankProfile("SBI", "State Bank of India", "SBIN", ("state bank of india", "sbi", "sbin0")),
    BankProfile("HDFC", "HDFC Bank", "HDFC", ("hdfc bank", "hdfc0", "housing development finance")),
    BankProfile("ICICI", "ICICI Bank", "ICIC", ("icici bank", "icic0")),
    BankProfile("AXIS", "Axis Bank", "UTIB", ("axis bank", "utib0")),
    BankProfile("KOTAK", "Kotak Mahindra Bank", "KKBK", ("kotak mahindra", "kkbk0", "kotak bank")),
    BankProfile("PNB", "Punjab National Bank", "PUNB", ("punjab national bank", "punb0", "pnb")),
    BankProfile("BOB", "Bank of Baroda", "BARB", ("bank of baroda", "barb0")),
    BankProfile("CANARA", "Canara Bank", "CNRB", ("canara bank", "cnrb0")),
    BankProfile("UNION", "Union Bank of India", "UBIN", ("union bank of india", "ubin0")),
    BankProfile("INDUS", "IndusInd Bank", "INDB", ("indusind bank", "indb0")),
    BankProfile("YES", "Yes Bank", "YESB", ("yes bank", "yesb0")),
    BankProfile("IDFC", "IDFC First Bank", "IDFB", ("idfc first", "idfb0", "idfc bank")),
    BankProfile("FEDERAL", "Federal Bank", "FDRL", ("federal bank", "fdrl0")),
    BankProfile("INDIAN", "Indian Bank", "IDIB", ("indian bank", "idib0")),
    BankProfile("BOI", "Bank of India", "BKID", ("bank of india", "bkid0")),
    BankProfile("CBI", "Central Bank of India", "CBIN", ("central bank of india", "cbin0")),
    BankProfile("IOB", "Indian Overseas Bank", "IOBA", ("indian overseas bank", "ioba0")),
    BankProfile("UCO", "UCO Bank", "UCBA", ("uco bank", "ucba0")),
    BankProfile("BOM", "Bank of Maharashtra", "MAHB", ("bank of maharashtra", "mahb0")),
    BankProfile("PSB", "Punjab & Sind Bank", "PSIB", ("punjab & sind bank", "punjab and sind", "psib0")),
    BankProfile("RBL", "RBL Bank", "RATN", ("rbl bank", "ratnakar", "ratn0")),
    BankProfile("BANDHAN", "Bandhan Bank", "BDBL", ("bandhan bank", "bdbl0")),
    BankProfile("IDBI", "IDBI Bank", "IBKL", ("idbi bank", "ibkl0")),
    BankProfile("SOUTHIND", "South Indian Bank", "SIBL", ("south indian bank", "sibl0")),
    BankProfile("KVB", "Karur Vysya Bank", "KVBL", ("karur vysya", "kvbl0")),
    BankProfile("CUB", "City Union Bank", "CIUB", ("city union bank", "ciub0")),
    BankProfile("DCB", "DCB Bank", "DCBL", ("dcb bank", "dcbl0")),
    BankProfile("KARNATAKA", "Karnataka Bank", "KARB", ("karnataka bank", "karb0")),
    BankProfile("TMB", "Tamilnad Mercantile Bank", "TMBL", ("tamilnad mercantile", "tmbl0")),
    BankProfile("CSB", "CSB Bank", "CSBK", ("csb bank", "catholic syrian", "csbk0")),
    BankProfile("DHANLAXMI", "Dhanlaxmi Bank", "DLXB", ("dhanlaxmi bank", "dlxb0")),
    BankProfile("JKBANK", "Jammu & Kashmir Bank", "JAKA", ("jammu and kashmir bank", "j&k bank", "jaka0")),
    BankProfile("NAINITAL", "Nainital Bank", "NTBL", ("nainital bank", "ntbl0")),
    BankProfile("AU", "AU Small Finance Bank", "AUBL", ("au small finance", "aubl0")),
    BankProfile("EQUITAS", "Equitas Small Finance Bank", "ESFB", ("equitas small finance", "esfb0")),
    BankProfile("UJJIVAN", "Ujjivan Small Finance Bank", "UJVN", ("ujjivan small finance", "ujvn0")),
    BankProfile("JANA", "Jana Small Finance Bank", "JSFB", ("jana small finance", "jsfb0")),
    BankProfile("SURYODAY", "Suryoday Small Finance Bank", "SURY", ("suryoday small finance", "sury0")),
    BankProfile("UTKARSH", "Utkarsh Small Finance Bank", "UTKS", ("utkarsh small finance", "utks0")),
    BankProfile("ESAF", "ESAF Small Finance Bank", "ESMF", ("esaf small finance", "esmf0")),
    BankProfile("FINCARE", "Fincare Small Finance Bank", "FSFB", ("fincare small finance", "fsfb0")),
    BankProfile("NESFB", "North East Small Finance Bank", "NESF", ("north east small finance", "nesf0")),
    BankProfile("SHIVALIK", "Shivalik Small Finance Bank", "SMCB", ("shivalik small finance", "smcb0")),
    BankProfile("UNITY", "Unity Small Finance Bank", "UNBA", ("unity small finance", "unba0")),
    BankProfile("CAPITAL", "Capital Small Finance Bank", "CLBL", ("capital small finance", "clbl0")),
    BankProfile("PAYTM", "Paytm Payments Bank", "PYTM", ("paytm payments bank", "pytm0")),
    BankProfile("AIRTEL", "Airtel Payments Bank", "AIRP", ("airtel payments bank", "airp0")),
    BankProfile("IPPB", "India Post Payments Bank", "IPOS", ("india post payments", "ipos0")),
    BankProfile("FINO", "Fino Payments Bank", "FINO", ("fino payments bank", "fino0")),
    BankProfile("JIO", "Jio Payments Bank", "JIOP", ("jio payments bank", "jiop0")),
    BankProfile("NSDL", "NSDL Payments Bank", "NSPB", ("nsdl payments bank", "nspb0")),
    BankProfile("SARASWAT", "Saraswat Cooperative Bank", "SRCB", ("saraswat co-operative", "saraswat bank", "srcb0")),
    BankProfile("COSMOS", "Cosmos Co-operative Bank", "COSB", ("cosmos co-op", "cosmos bank", "cosb0")),
    BankProfile("SVC", "SVC Co-operative Bank", "SVCB", ("svc co-operative", "shamrao vithal", "svcb0")),
    BankProfile("ABHYUDAYA", "Abhyudaya Co-operative Bank", "ABHY", ("abhyudaya co-op", "abhy0")),
    BankProfile("TJSB", "TJSB Sahakari Bank", "TJSB", ("tjsb sahakari", "thane janata", "tjsb0")),
    BankProfile("NKGSB", "NKGSB Co-operative Bank", "NKGS", ("nkgsb co-op", "nkgs0")),
    BankProfile("BHARATCOOP", "Bharat Co-operative Bank", "BCBM", ("bharat co-operative", "bcbm0")),
    BankProfile("KALUPUR", "Kalupur Commercial Co-op Bank", "KCCB", ("kalupur commercial", "kccb0")),
    BankProfile("MEHSANA", "Mehsana Urban Co-op Bank", "MSNU", ("mehsana urban", "msnu0")),
    BankProfile("RAJKOT", "Rajkot Nagarik Sahakari Bank", "RNSB", ("rajkot nagarik", "rnsb0")),
    BankProfile("APMC", "Ahmedabad Mercantile Co-op Bank", "AMCB", ("ahmedabad mercantile", "amcb0")),
    BankProfile("GP", "Gopinath Patil Parsik Janata Bank", "PJSB", ("parsik janata", "pjsb0")),
    BankProfile("DOMBIVLI", "Dombivli Nagari Sahakari Bank", "DNSB", ("dombivli nagari", "dnsb0")),
    BankProfile("KALYAN", "Kalyan Janata Sahakari Bank", "KJSB", ("kalyan janata", "kjsb0")),
    BankProfile("JANATA", "Janata Sahakari Bank Pune", "JSBP", ("janata sahakari", "jsbp0")),
    BankProfile("MAHANAGAR", "Mahanagar Co-operative Bank", "MCBL", ("mahanagar co-op", "mcbl0")),
    BankProfile("BOMBAY", "Bombay Mercantile Co-op Bank", "BMCB", ("bombay mercantile", "bmcb0")),
    BankProfile("CITIZEN", "Citizen Credit Co-op Bank", "CCBL", ("citizen credit", "ccbl0")),
    BankProfile("NEWINDIA", "New India Co-operative Bank", "NICB", ("new india co-op", "nicb0")),
    BankProfile("ZOROASTRIAN", "Zoroastrian Co-op Bank", "ZCBL", ("zoroastrian co-op", "zcbl0")),
    BankProfile("HSBC", "HSBC Bank India", "HSBC", ("hsbc", "hongkong and shanghai")),
    BankProfile("SCB", "Standard Chartered Bank", "SCBL", ("standard chartered", "scbl0")),
    BankProfile("CITI", "Citibank India", "CITI", ("citibank", "citi0")),
    BankProfile("DBS", "DBS Bank India", "DBSS", ("dbs bank", "dbss0", "lakshmi vilas")),
    BankProfile("DEUTSCHE", "Deutsche Bank India", "DEUT", ("deutsche bank", "deut0")),
    BankProfile("BARCLAYS", "Barclays Bank India", "BARC", ("barclays bank", "barc0")),
    BankProfile("BOFA", "Bank of America India", "BOFA", ("bank of america", "bofa0")),
    BankProfile("JPM", "JP Morgan Chase Bank India", "CHAS", ("jp morgan", "chas0")),
    BankProfile("BNP", "BNP Paribas India", "BNPA", ("bnp paribas", "bnpa0")),
    BankProfile("MUFG", "MUFG Bank India", "BOTM", ("mufg bank", "botm0")),
    BankProfile("MIZUHO", "Mizuho Bank India", "MHCB", ("mizuho bank", "mhcb0")),
    BankProfile("SMBC", "Sumitomo Mitsui Banking Corp", "SMBC", ("sumitomo mitsui", "smbc0")),
    BankProfile("DOHA", "Doha Bank India", "DOHB", ("doha bank", "dohb0")),
    BankProfile("QNB", "Qatar National Bank India", "QNBA", ("qatar national", "qnba0")),
    BankProfile("SHINHAN", "Shinhan Bank India", "SHBK", ("shinhan bank", "shbk0")),
    BankProfile("KEB", "KEB Hana Bank India", "KOEX", ("keb hana", "koex0")),
    BankProfile("WOORI", "Woori Bank India", "HVBK", ("woori bank", "hvbk0")),
    BankProfile("RABOBANK", "Rabobank International", "RABO", ("rabobank", "rabo0")),
    BankProfile("APGB", "Andhra Pragathi Grameena Bank", "APGB", ("andhra pragathi", "apgb0")),
    BankProfile("APGVB", "Andhra Pradesh Grameena Vikas Bank", "APGV", ("grameena vikas", "apgv0")),
    BankProfile("BGGB", "Baroda Gujarat Gramin Bank", "BGGB", ("baroda gujarat gramin", "bggb0")),
    BankProfile("BRKGB", "Baroda Rajasthan Kshetriya Gramin Bank", "BRKG", ("baroda rajasthan", "brkg0")),
    BankProfile("BUPB", "Baroda UP Bank", "BARB0BUPGBX", ("baroda up bank", "bupb")),
    BankProfile("CGGB", "Chaitanya Godavari Grameena Bank", "CGGB", ("chaitanya godavari", "cggb0")),
    BankProfile("CRGB", "Chhattisgarh Rajya Gramin Bank", "CRGB", ("chhattisgarh rajya gramin", "crgb0")),
    BankProfile("KGB", "Kerala Gramin Bank", "KLGB", ("kerala gramin", "klgb0")),
    BankProfile("KVGB", "Karnataka Vikas Grameena Bank", "KVGB", ("karnataka vikas grameena", "kvgb0")),
    BankProfile("KRGB", "Karnataka Gramin Bank", "PKGB", ("karnataka gramin", "pkgb0")),
    BankProfile("MGB", "Maharashtra Gramin Bank", "MAHG", ("maharashtra gramin", "mahg0")),
    BankProfile("MPGB", "Madhya Pradesh Gramin Bank", "MPGB", ("madhya pradesh gramin", "mpgb0")),
    BankProfile("PBGB", "Paschim Banga Gramin Bank", "PBGB", ("paschim banga gramin", "pbgb0")),
    BankProfile("PRATHAMA", "Prathama UP Gramin Bank", "PUPB", ("prathama up gramin", "pupb0")),
    BankProfile("RMGB", "Rajasthan Marudhara Gramin Bank", "RMGB", ("rajasthan marudhara", "rmgb0")),
    BankProfile("SGB", "Sarva Haryana Gramin Bank", "SHGB", ("sarva haryana gramin", "shgb0")),
    BankProfile("TGB", "Telangana Grameena Bank", "TGB", ("telangana grameena", "sbhy0")),
    BankProfile("VKGB", "Vidharbha Konkan Gramin Bank", "BKID0VAIGB", ("vidharbha konkan", "vkgb")),
    BankProfile("APCOB", "Andhra Pradesh State Co-op Bank", "APBL", ("ap state co-op", "apbl0")),
    BankProfile("MSC", "Maharashtra State Co-op Bank", "MSCI", ("maharashtra state co-op", "msci0")),
    BankProfile("GSC", "Gujarat State Co-op Bank", "GSCB", ("gujarat state co-op", "gscb0")),
    BankProfile("KSC", "Karnataka State Co-op Apex Bank", "KSCB", ("karnataka state co-op", "kscb0")),
    BankProfile("TNSC", "Tamil Nadu State Apex Co-op Bank", "TNSC", ("tamil nadu state apex", "tnsc0")),
    BankProfile("DELHI", "Delhi State Co-op Bank", "DLSC", ("delhi state co-op", "dlsc0")),
]

BANK_BY_CODE: Dict[str, BankProfile] = {b.bank_code: b for b in INDIAN_BANKS}


# Curated Indian banking terms per category. These are matched on token boundaries by the categoriser
# (so "zomato" also covers "UPI/ZOMATO", "zomato@okicici" and "ZOMATO LTD"); no generated rail/VPA/bank
# permutations are added and the reported knowledge-base size is the number of distinct curated terms.
_BASE_CATEGORY_PATTERNS: Dict[int, List[str]] = {
    1: [
        "salary", "sal cr", "payroll", "monthly sal", "wages", "stipend cr",
        "emp sal", "salary credit", "neft sal", "tcs salary", "infosys salary",
        "wipro salary", "accenture salary", "cognizant salary", "hcl salary",
        "reliance salary", "hdfc salary", "icici salary", "pension credit",
    ],
    2: [
        "razorpay", "cashfree", "payu", "billdesk", "ccavenue", "bharatpe",
        "phonepe merchant", "gpay business", "paytm merchant", "pine labs",
        "mswipe", "ezetap", "settlement", "pos cr", "sales receipt",
        "invoice settlement", "merchant payout", "trade receipt", "cod settlement",
        "amazon seller", "flipkart seller", "meesho payout", "shopify payout",
    ],
    3: [
        "consultancy", "professional fee", "retainer fee", "advisory fee",
        "audit fee cr", "legal fee cr", "freelance", "design fee",
        "technical service", "software consulting", "architect fee",
    ],
    4: [
        "int.pd", "interest credit", "int cr", "savings interest", "sb int",
        "quarterly interest", "fd interest", "td int", "sweep interest",
        "credit interest", "int. on sb", "interest paid by bank",
    ],
    5: [
        "dividend", "div cr", "ach div", "ecs div", "interim dividend",
        "final dividend", "iccl dividend", "ncl dividend", "cdsl dividend",
        "nsdl dividend", "capital gain payout",
    ],
    6: [
        "rent received", "house rent cr", "lease rent", "rental income",
        "nobroker rent cr", "tenant rent", "shop rent cr", "godown rent cr",
    ],
    7: [
        "it refund", "income tax refund", "cbdt refund", "cpc bangalore",
        "tin nsdl refund", "gst refund", "igst refund", "tax refund",
        "sbi govt refund", "assessment refund",
    ],
    8: [
        "loan disbursal", "disbursement", "od limit cr", "term loan cr",
        "bajaj finance cr", "tata capital cr", "hdfc loan cr", "mudra loan",
        "working capital cr", "credit line cr", "kreditbee cr", "navi loan cr",
    ],
    9: [
        "capital infusion", "director loan", "partner capital", "share application",
        "equity infusion", "promoter fund", "seed capital", "angel fund",
    ],
    10: [
        "commission cr", "brokerage cr", "dsa payout", "incentive cr",
        "referral bonus", "agency commission", "lic commission", "affiliate payout",
    ],
    11: [
        "vendor pmt", "supplier", "traders", "enterprises", "industries",
        "distributors", "wholesalers", "agencies", "corporates", "impex",
        "infra", "buildcon", "hardware", "steels", "polymers", "chemicals",
    ],
    12: [
        "raw material", "inventory", "stock purchase", "goods purchase",
        "textile", "fabrics", "packaging", "spare parts", "electronics wholesale",
    ],
    13: [
        "office rent", "rent paid", "warehouse rent", "lease payment",
        "co-working", "wework", "awfis", "regus", "society maintenance",
        "nobroker pay", "cred rent",
    ],
    14: [
        "staff salary", "payroll dr", "salary paid", "wages dr", "intern stipend",
        "bonus paid", "gratuity", "leave encashment", "keka payroll", "razorpayx payroll",
    ],
    15: [
        "bescom", "msedcl", "mahavitaran", "adani electricity", "tata power",
        "tpddl", "bses", "cesc", "torrent power", "ugvcl", "dgvcl", "pgvcl",
        "mgvcl", "dhbvn", "uhbvn", "jvvnl", "avvnl", "tsspdcl", "tangedco",
        "kseb", "wbsedcl", "uppcl", "electricity bill", "water bill", "bwssb",
        "mahanagar gas", "igl", "gujarat gas", "indane gas", "hp gas", "bharat gas",
    ],
    16: [
        "airtel", "jio", "reliance jio", "vodafone", "vi post", "bsnl",
        "mtnl", "act fibernet", "hathway", "tataplay", "dish tv", "d2h",
        "broadband", "mobile recharge", "postpaid", "fiber bill",
    ],
    17: [
        "aws", "amazon web services", "google cloud", "microsoft azure",
        "zoho", "tally solutions", "freshworks", "slack", "zoom", "adobe",
        "github", "atlassian", "openai", "godaddy", "cloudflare", "digitalocean",
        "canva", "notion", "microsoft 365", "google workspace",
    ],
    18: [
        "chartered accountant", "ca firm", "advocate", "legal fee",
        "solicitor", "law firm", "audit fee", "company secretary", "cs fee",
        "tax consultant", "vakilsearch", "cleartax", "indiafilings",
    ],
    19: [
        "google ads", "facebook ads", "meta ads", "instagram ads", "linkedin ads",
        "marketing", "advertising", "digital agency", "seo agency", "pr agency",
        "hoarding", "sponsorship",
    ],
    20: [
        "delhivery", "bluedart", "dtdc", "fedex", "dhl", "ecom express",
        "xpressbees", "porter", "shiprocket", "shadowfax", "gati", "vrl logistics",
        "tci freight", "rivigo", "safexpress", "courier", "freight", "transport",
    ],
    21: [
        "stationery", "printing", "xerox", "office supplies", "paper mart",
        "staples", "toner", "cartridge", "courier stationery",
    ],
    22: [
        "amc charge", "repair", "carpenter", "electrician", "plumber",
        "ac service", "urban company", "facility management", "housekeeping",
        "pest control", "security agency",
    ],
    23: [
        "home loan", "housing finance", "hdfc ltd emi", "lic housing", "lichfl",
        "pnb housing", "indiabulls housing", "aadhar housing", "aavas financiers",
        "aptus value", "can fin homes", "repco home", "mortgage emi",
    ],
    24: [
        "auto loan", "car loan", "vehicle loan", "maruti finance", "mahindra finance",
        "cholamandalam", "sundaram finance", "shriram finance", "tvs credit",
        "hero fincorp", "kotak prime", "toyota financial", "two wheeler loan",
    ],
    25: [
        "bajaj finserv", "bajaj finance", "nach emi", "ecs emi", "ach loan",
        "tata capital", "fullerton", "smfg india", "aditya birla finance",
        "l&t finance", "piramal finance", "iifl finance", "muthoot finance",
        "manappuram", "kreditbee", "moneyview", "navi finserv", "fibe", "earlysalary",
        "lazypay", "simpl", "kissht", "ring loan", "dhani", "loan emi",
    ],
    26: [
        "credit card", "cc bill", "cred club", "cred pay", "sbi card",
        "hdfc card", "icici card", "axis card", "amex", "american express",
        "rbl card", "kotak card", "indusind card", "onecard", "scb card",
    ],
    27: [
        "sms chg", "sms alert", "min bal", "amb charge", "mab chg",
        "non maintenance", "debit card fee", "annual fee", "atm card chg",
        "folio chg", "ledger folio", "dd charges", "imps chg", "neft chg",
        "rtgs chg", "service charge", "gst on chg", "bank charges", "atm fee",
    ],
    28: [
        "chq return", "cheque return", "inward return", "outward return",
        "ecs return", "nach return", "ach return", "insufficient funds",
        "insuff bal", "bounce chg", "dishonour chg", "si fail chg", "ecs rtn",
        "nach rtn", "ach rtn",
    ],
    29: [
        "od interest", "cc interest", "overdraft int", "penal interest",
        "processing fee", "tod charge", "excess interest", "loan interest dr",
        "commitment charge", "renewal fee",
    ],
    30: [
        "forex", "lrs remittance", "swift transfer", "tt remittance",
        "foreign exchange", "bookmyforex", "thomas cook", "niyo global",
        "wire transfer", "outward remittance", "fema", "tcs on lrs",
    ],
    31: [
        "mutual fund", "sip", "cams", "kfintech", "bse star mf", "nse mf",
        "iccl mf", "nippon mf", "sbi mf", "hdfc mf", "icici pru mf",
        "axis mf", "mirae asset", "parag parikh", "ppfas", "quant mf",
        "kuvera", "coin zerodha", "groww mf", "et money", "paytm money",
    ],
    32: [
        "zerodha", "groww", "upstox", "angel one", "angel broking",
        "icici direct", "hdfc securities", "kotak securities", "motilal oswal",
        "sharekhan", "5paisa", "dhan", "fyers", "alice blue", "iifl securities",
        "nse clearing", "bse clearing", "demat", "nsecl",
    ],
    33: [
        "fd creation", "fixed deposit open", "td creation", "rd installment",
        "recurring deposit", "auto sweep", "mod sweep", "term deposit dr",
    ],
    34: [
        "fd maturity", "td maturity", "fd closure", "rd maturity",
        "sweep reverse", "mod closure", "term deposit cr", "investment maturity",
    ],
    35: [
        "lic of india", "life insurance", "hdfc life", "icici pru life",
        "sbi life", "max life", "bajaj allianz", "tata aia", "kotak life",
        "star health", "niva bupa", "care health", "hdfc ergo", "icici lombard",
        "new india assurance", "oriental insurance", "acko", "digit insurance", "policybazaar",
        "lic premium", "lic prem",
    ],
    36: [
        "epfo", "provident fund", "ppf deposit", "public provident",
        "nps contribution", "national pension", "pfrda", "cra nsdl",
        "atal pension", "apy", "sukanya samriddhi",
    ],
    37: [
        "coindcx", "wazirx", "coinswitch", "zebpay", "giottus", "unocoin",
        "mudrex", "bitbns", "buyucoin", "binance", "coinbase", "okx",
        "bybit", "kraken", "crypto", "vda exchange", "virtual digital",
    ],
    38: [
        "income tax", "advance tax", "self assessment tax", "cbdt",
        "nsdl tax", "oltas", "tin 2.0", "tds payment", "challan 280",
        "challan 281", "efiling tax", "protean tax",
    ],
    39: [
        "gst payment", "gstn", "pmt-06", "pmt06", "cgst", "sgst", "igst",
        "goods and services tax", "gst portal", "icegate gst",
    ],
    40: [
        "professional tax", "ptax", "property tax", "municipal", "bmcl tax",
        "bmc property", "bbmp tax", "mcd tax", "gmcc tax", "pmc property",
        "gram panchayat", "water tax",
    ],
    41: [
        "customs duty", "icegate", "import duty", "export duty",
        "port trust", "jnpt", "dgft", "custom house",
    ],
    42: [
        "mca21", "roc fee", "stamp duty", "gras", "egov challan", "parivahan",
        "echallan", "traffic fine", "court fee", "sebi fee", "rbi penalty",
        "passport seva", "uidai", "municipal fine",
    ],
    43: [
        "zomato", "swiggy", "dominos", "mcdonalds", "kfc", "pizza hut",
        "burger king", "starbucks", "cafe coffee day", "haldiram", "barbeque nation",
        "eatsure", "magicpin", "dineout", "restaurant", "bakery", "cafe", "bistro",
    ],
    44: [
        "blinkit", "zepto", "instamart", "bigbasket", "dmart", "avenue supermarts",
        "reliance fresh", "reliance smart", "spencers", "more supermarket",
        "nature basket", "jiomart", "milk basket", "country delight",
        "kirana", "supermarket", "grofers", "dairy", "fruits and veg",
    ],
    45: [
        "indian oil", "iocl", "bharat petroleum", "bpcl", "hindustan petroleum",
        "hpcl", "nayara", "jio bp", "shell petrol", "petrol pump", "fuel",
        "uber", "ola", "rapido", "namma yatri", "fastag", "nhai toll",
        "metro rail", "dmrc", "mmrc", "bmtc", "best bus", "ather energy",
    ],
    46: [
        "indigo", "air india", "vistara", "akasa air", "spicejet",
        "irctc", "makemytrip", "goibibo", "cleartrip", "yatra", "easemytrip",
        "ixigo", "redbus", "oyo", "taj hotels", "marriott", "oberoi",
        "airbnb", "booking.com", "agoda", "lemon tree", "itc hotels",
    ],
    47: [
        "apollo pharmacy", "medplus", "pharmeasy", "1mg", "tata 1mg",
        "netmeds", "practo", "apollo hospital", "fortis", "max healthcare",
        "manipal hospital", "medanta", "narayana health", "lilavati",
        "kokilaben", "thyrocare", "lal pathlabs", "metropolis", "clinic", "hospital", "pharmacy",
        "medical store", "medicals", "chemist", "chemists", "diagnostics", "nursing home",
    ],
    48: [
        "amazon", "flipkart", "myntra", "ajio", "nykaa", "meesho",
        "tata cliq", "croma", "reliance digital", "vijay sales", "decathlon",
        "zara", "h&m", "uniqlo", "westside", "pantaloons", "shoppers stop",
        "lifestyle store", "lenskart", "titan", "tanishq", "ikea", "pepperfry",
    ],
    49: [
        "self transfer", "own account", "trf to self", "interbank self",
        "sweep transfer", "contra trf", "internal trf", "self account",
        "own a/c", "same pan trf", "self trf",
    ],
    50: [
        "cash deposit", "cash dep", "by cash", "cdm deposit", "bnq dep",
        "cash cr", "branch cash dep", "bna deposit", "recycler cash",
    ],
    51: [
        "atm wdl", "atm withdrawal", "cash withdrawal", "cash wdl",
        "to cash", "aeps", "aadhaar pay", "self chq cash", "bearer chq",
        "nfs atm", "atw/", "cwl/", "atm-", "cardless cash",
    ],
}


# P1 growth of the curated KB (NLP-03): generic purpose phrases and institutions that the first KB lacked,
# found by error analysis on the development split of the labelled corpus (tests/fixtures/nlp). Every entry
# is a real-world narration phrase; patterns are matched on token boundaries per category direction, and
# patterns of four characters or fewer only ever match a whole token.
_KB_SUPPLEMENT_P1: Dict[int, List[str]] = {
    1: ["sal", "salary for", "sal jun", "payroll credit"],
    2: ["sales", "sales inv", "against bill", "against invoice", "bill payment received", "sale proceeds"],
    3: ["professional fees", "prof fees", "consulting charges", "consultancy fee", "retainership", "audit fee", "audit fees"],
    4: ["interest credited", "int on fd", "interest on fd", "int on sb"],
    5: ["div", "mf redemption", "redemption proceeds"],
    6: ["shop rent", "lease rental", "rent received for", "rental"],
    7: ["itd refund", "refund of gst", "it refund ay"],
    8: ["loan disb", "disb", "drawdown", "loan disbursement"],
    9: ["promoter contribution", "unsecured loan from director", "capital contribution"],
    10: ["commission", "brokerage payout", "incentive", "dsa commission", "agent commission"],
    11: ["vendor payment", "supplier payment", "contractor", "labour charges", "job work"],
    12: ["purchase order", "purchase of goods", "goods purchase", "cotton bales", "wholesale", "yarn"],
    13: ["rent for shop", "nobroker", "common area maintenance", "warehouse lease", "lease rental"],
    14: ["salary", "wages", "bulk salary", "salary advance", "salary transfer", "employees"],
    15: ["indraprastha gas", "piped gas", "gas bill", "electricity"],
    16: ["tata play", "dth", "dth recharge", "prepaid", "landline"],
    18: ["audit fees", "legal fees", "professional fees", "consultancy charges", "consultancy", "roc filing"],
    19: ["justdial", "indiamart", "advertisement", "ads", "campaign"],
    20: ["blue dart", "courier charges", "freight charges", "transport charges"],
    21: ["classmate", "notebooks", "printing charges", "flex banner"],
    22: ["repairs", "servicing", "annual maintenance contract", "ac repair", "repair work"],
    23: ["housing loan", "home loan emi"],
    27: ["atm txn charges", "cheque book", "chq book", "account maintenance charges"],
    28: ["bounce", "return charges", "return chg", "mandate failed", "debit return"],
    29: ["interest on overdraft", "int debited", "cash credit interest", "documentation charges", "od int"],
    30: ["forex markup", "swift charges", "inward remittance", "foreign remittance", "wise payments", "foreign inward"],
    32: ["icici securities", "securities payout", "trading account"],
    33: ["term deposit booked", "fixed deposit opened", "fixed deposit", "fd booking"],
    34: ["premature closure", "bond maturity", "deposit closure"],
    38: ["tds on rent", "protean tds", "tds challan"],
    39: ["gst challan", "gst late fee"],
    41: ["iec", "customs"],
    42: ["roc filing fee", "court fee stamp", "rc transfer"],
}
for _cid, _terms in _KB_SUPPLEMENT_P1.items():
    _BASE_CATEGORY_PATTERNS.setdefault(_cid, []).extend(_terms)

# Categories whose KB terms describe the event itself and outrank the institution named alongside it
# ("NACH RETURN CHARGES BAJAJ FINANCE" is a bounce charge, not an EMI). Confidence bonus on a KB hit.
KB_EVENT_PRIORITY: Dict[int, float] = {28: 0.02, 29: 0.01}


# Curated tabular-feature priors for the ML tier (NLP-15): pseudo-counts of discretised tabular tokens
# (see categorizer.tabular_feature_tokens) per category. They only tilt the text score of the Naive Bayes
# tier; a refit on analyst-labelled rows adds observed counts on top.
TABULAR_FEATURE_PRIORS: Dict[str, Dict[int, int]] = {
    "__ch_nach": {23: 3, 24: 3, 25: 3, 26: 2, 31: 3, 35: 3, 36: 2, 5: 1, 1: 1},
    "__mandate": {23: 3, 24: 3, 25: 3, 26: 1, 31: 3, 35: 3, 36: 2, 16: 1, 17: 1},
    "__ch_pos": {43: 2, 44: 2, 45: 2, 46: 2, 47: 2, 48: 2, 17: 1, 21: 1},
    "__ch_cheque": {2: 1, 11: 1, 12: 1, 13: 1, 18: 1},
    "__ch_bank_charge": {27: 3, 28: 3, 29: 2},
    "__ch_interest": {4: 3, 29: 1},
    "__ch_forex": {30: 3},
    "__ch_rtgs": {2: 1, 11: 1, 12: 1, 8: 1, 9: 1},
    "__dom_early": {1: 2, 6: 1, 13: 1, 23: 1, 24: 1, 25: 1, 14: 1},
    "__dom_late": {1: 1, 14: 1},
    "__amt_round": {6: 1, 8: 1, 9: 1, 13: 1, 33: 1, 31: 1},
    "__amt_b1": {27: 1, 16: 1, 45: 1, 43: 1},
    "__amt_b2": {27: 1, 43: 1, 44: 1, 45: 1, 16: 1},
    "__amt_b5": {8: 1, 9: 1, 12: 1, 2: 1},
    "__amt_b6": {8: 1, 9: 1, 12: 1, 2: 1},
    "__amt_b7": {8: 1, 9: 1, 12: 1, 2: 1},
}


def build_knowledge_base() -> Tuple[Dict[int, List[str]], int]:
    """
    Normalise the curated knowledge base: lower-case, collapse whitespace, de-duplicate within a category.
    Returns (category_id -> pattern list, number of curated terms). No synthetic variants are generated.
    """
    kb: Dict[int, List[str]] = {}
    total = 0
    for cat_id, base_terms in _BASE_CATEGORY_PATTERNS.items():
        if cat_id not in CATEGORY_BY_ID:
            raise ValueError(f"Knowledge base references unknown category {cat_id}")
        seen: set = set()
        terms: List[str] = []
        for term in base_terms:
            clean = " ".join(term.strip().lower().split())
            if clean and clean not in seen:
                seen.add(clean)
                terms.append(clean)
        kb[cat_id] = terms
        total += len(terms)
    return kb, total


KNOWLEDGE_BASE_PATTERNS, KNOWLEDGE_BASE_POINT_COUNT = build_knowledge_base()


def curated_knowledge_base_count() -> int:
    """Honest size of the curated knowledge base (distinct terms summed over categories)."""
    return sum(len(v) for v in KNOWLEDGE_BASE_PATTERNS.values())


def knowledge_base_stats() -> Dict[str, int]:
    """Summary for API/UI display: curated term count, distinct terms and categories covered."""
    distinct = {p for pats in KNOWLEDGE_BASE_PATTERNS.values() for p in pats}
    return {
        "curated_terms": curated_knowledge_base_count(),
        "distinct_terms": len(distinct),
        "categories_covered": sum(1 for v in KNOWLEDGE_BASE_PATTERNS.values() if v),
    }
