from datetime import datetime
import re

NS = {"ns": "urn:OECD:StandardAuditFile-Tax:PT_1.04_01"}

IVA_REGRAS = {
    "NOR": 23.0,
    "INT": 13.0,
    "RED": 6.0,
    "ISE": 0.0,
    "OUT": 0.0,
}

# ======================================================
# UTIL
# ======================================================

def _validar_nif_pt(nif: str) -> bool:
    if not re.fullmatch(r"\d{9}", nif):
        return False
    if nif[0] not in "1235689":
        return False
    total = sum(int(nif[i]) * (9 - i) for i in range(8))
    check = 11 - (total % 11)
    if check >= 10:
        check = 0
    return check == int(nif[8])

_ATCUD_RE = re.compile(r"^(?:ATCUD:\s*)?([A-Za-z0-9]{8,})-([0-9]+)$")

def _ftext(el):
    return el.text.strip() if el is not None and el.text and el.text.strip() else None

def _float(el):
    return float(_ftext(el))

# ======================================================
# PASSO 1 — HEADER / DATAS / NIF
# ======================================================

def validar_header(xml_root, errors):
    header = xml_root.find("ns:Header", namespaces=NS)
    if header is None:
        errors.append("Header em falta.")
        return

    obrigatorios = [
        "AuditFileVersion",
        "CompanyID",
        "TaxRegistrationNumber",
        "CompanyName",
        "FiscalYear",
        "StartDate",
        "EndDate",
        "CurrencyCode",
        "DateCreated",
    ]

    for campo in obrigatorios:
        el = header.find(f"ns:{campo}", namespaces=NS)
        if _ftext(el) is None:
            errors.append(f"Header.{campo} é obrigatório.")

def validar_datas(xml_root, errors):
    header = xml_root.find("ns:Header", namespaces=NS)
    if header is None:
        return
    try:
        fy = int(_ftext(header.find("ns:FiscalYear", namespaces=NS)))
        start = datetime.fromisoformat(_ftext(header.find("ns:StartDate", namespaces=NS)))
        end = datetime.fromisoformat(_ftext(header.find("ns:EndDate", namespaces=NS)))
        if start > end:
            errors.append("StartDate não pode ser maior que EndDate.")
        if start.year != fy:
            errors.append("FiscalYear não corresponde ao ano de StartDate.")
    except Exception:
        errors.append("Datas fiscais inválidas no Header.")

def validar_nif(xml_root, errors):
    header = xml_root.find("ns:Header", namespaces=NS)
    if header is None:
        return
    nif_el = header.find("ns:TaxRegistrationNumber", namespaces=NS)
    nif = _ftext(nif_el)
    if nif is None:
        return
    if not _validar_nif_pt(nif):
        errors.append("NIF inválido no Header.")

# ======================================================
# PASSO 2 — IVA / TAXTABLE
# ======================================================

def validar_tax_table(xml_root, errors):
    tax_table = xml_root.find("ns:MasterFiles/ns:TaxTable", namespaces=NS)
    if tax_table is None:
        errors.append("TaxTable em falta.")
        return

    entries = tax_table.findall("ns:TaxTableEntry", namespaces=NS)
    if not entries:
        errors.append("TaxTable sem TaxTableEntry.")
        return

    vistos = {}
    for entry in entries:
        tax_type = _ftext(entry.find("ns:TaxType", namespaces=NS))
        region = _ftext(entry.find("ns:TaxCountryRegion", namespaces=NS))
        code = _ftext(entry.find("ns:TaxCode", namespaces=NS))
        perc_el = entry.find("ns:TaxPercentage", namespaces=NS)

        if None in (tax_type, region, code) or _ftext(perc_el) is None:
            errors.append("TaxTableEntry incompleto.")
            continue

        if tax_type != "IVA" or region != "PT":
            continue

        try:
            tax_perc = float(_ftext(perc_el))
        except Exception:
            errors.append(f"IVA {code}: TaxPercentage inválido.")
            continue

        if code not in IVA_REGRAS:
            errors.append(f"IVA TaxCode desconhecido: {code}.")
            continue

        if abs(tax_perc - IVA_REGRAS[code]) > 0.001:
            errors.append(f"IVA {code}: TaxPercentage inválido.")

        if code in vistos and vistos[code] != tax_perc:
            errors.append(f"IVA {code}: duplicado com percentagens diferentes.")
        vistos[code] = tax_perc

# ======================================================
# PASSO 3 — SALES INVOICES (totais)
# ======================================================

def validar_sales_invoices(xml_root, errors):
    sales = xml_root.find("ns:SourceDocuments/ns:SalesInvoices", namespaces=NS)
    if sales is None:
        errors.append("SalesInvoices em falta.")
        return
    invoices = sales.findall("ns:Invoice", namespaces=NS)
    if not invoices:
        errors.append("SalesInvoices sem Invoice.")
        return

    for inv in invoices:
        inv_no = _ftext(inv.find("ns:InvoiceNo", namespaces=NS))
        totals = inv.find("ns:DocumentTotals", namespaces=NS)
        if inv_no is None or totals is None:
            errors.append("Invoice incompleta.")
            continue
        try:
            net = _float(totals.find("ns:NetTotal", namespaces=NS))
            tax = _float(totals.find("ns:TaxPayable", namespaces=NS))
            gross = _float(totals.find("ns:GrossTotal", namespaces=NS))
        except Exception:
            errors.append(f"Invoice {inv_no}: totais inválidos.")
            continue
        if abs((net + tax) - gross) > 0.01:
            errors.append(f"Invoice {inv_no}: GrossTotal incoerente.")

# ======================================================
# PASSO 4 — ATCUD / HASH (INVOICES)
# ======================================================

def validar_atcud_hash_sales_invoices(xml_root, errors):
    sales = xml_root.find("ns:SourceDocuments/ns:SalesInvoices", namespaces=NS)
    if sales is None:
        return
    invoices = sales.findall("ns:Invoice", namespaces=NS)

    for inv in invoices:
        inv_no = _ftext(inv.find("ns:InvoiceNo", namespaces=NS)) or "<sem InvoiceNo>"
        inv_date_s = _ftext(inv.find("ns:InvoiceDate", namespaces=NS))
        if not inv_date_s:
            errors.append(f"Invoice {inv_no}: InvoiceDate em falta.")
            continue

        try:
            inv_date = datetime.fromisoformat(inv_date_s).date()
        except Exception:
            errors.append(f"Invoice {inv_no}: InvoiceDate inválido.")
            continue

        atcud_el = inv.find("ns:ATCUD", namespaces=NS)
        atcud = _ftext(atcud_el)

        if inv_date >= datetime(2023, 1, 1).date():
            if not atcud or not _ATCUD_RE.match(atcud):
                errors.append(f"Invoice {inv_no}: ATCUD inválido ou em falta.")

        hash_el = inv.find("ns:Hash", namespaces=NS)
        h = _ftext(hash_el)
        if not h:
            errors.append(f"Invoice {inv_no}: Hash em falta.")
            continue

        if h != "0":
            hc_el = inv.find("ns:HashControl", namespaces=NS)
            hc = _ftext(hc_el)
            if not hc or not re.fullmatch(r"\d+(\.\d+)?", hc):
                errors.append(f"Invoice {inv_no}: HashControl inválido.")

# ======================================================
# PASSO 5 — CUSTOMERS (existência vs invoices)
# ======================================================

def validar_customers(xml_root, errors):
    customers = xml_root.findall("ns:MasterFiles/ns:Customer", namespaces=NS)
    customer_ids = {}
    for c in customers:
        cid = _ftext(c.find("ns:CustomerID", namespaces=NS))
        if cid:
            customer_ids[cid] = c

    sales = xml_root.find("ns:SourceDocuments/ns:SalesInvoices", namespaces=NS)
    if sales is None:
        return

    for inv in sales.findall("ns:Invoice", namespaces=NS):
        inv_no = _ftext(inv.find("ns:InvoiceNo", namespaces=NS)) or "<sem InvoiceNo>"
        cid = _ftext(inv.find("ns:CustomerID", namespaces=NS))
        if cid and cid not in customer_ids:
            errors.append(f"Invoice {inv_no}: CustomerID {cid} não existe.")

# ======================================================
# PASSO 6 — PRODUCTS (existência vs linhas de invoices)
# ======================================================

def validar_products(xml_root, errors):
    products = xml_root.findall("ns:MasterFiles/ns:Product", namespaces=NS)
    product_codes = set()
    for p in products:
        pc = _ftext(p.find("ns:ProductCode", namespaces=NS))
        if pc:
            product_codes.add(pc)

    sales = xml_root.find("ns:SourceDocuments/ns:SalesInvoices", namespaces=NS)
    if sales is None:
        return

    for inv in sales.findall("ns:Invoice", namespaces=NS):
        inv_no = _ftext(inv.find("ns:InvoiceNo", namespaces=NS)) or "<sem InvoiceNo>"
        for line in inv.findall("ns:Line", namespaces=NS):
            pc = _ftext(line.find("ns:ProductCode", namespaces=NS))
            if not pc or pc not in product_codes:
                errors.append(f"Invoice {inv_no}: ProductCode inválido (não existe em MasterFiles).")

# ======================================================
# PASSO 7 — PAYMENTS (Hash opcional; ligação a invoice)
# ======================================================

def validar_payments(xml_root, errors):
    payments = xml_root.find("ns:SourceDocuments/ns:Payments", namespaces=NS)
    if payments is None:
        return

    pay_list = payments.findall("ns:Payment", namespaces=NS)
    if not pay_list:
        errors.append("Payments sem Payment.")
        return

    invoices = set()
    sales = xml_root.find("ns:SourceDocuments/ns:SalesInvoices", namespaces=NS)
    if sales is not None:
        for inv in sales.findall("ns:Invoice", namespaces=NS):
            ino = _ftext(inv.find("ns:InvoiceNo", namespaces=NS))
            if ino:
                invoices.add(ino)

    for pay in pay_list:
        ref = _ftext(pay.find("ns:PaymentRefNo", namespaces=NS)) or "<sem PaymentRefNo>"
        date_s = _ftext(pay.find("ns:TransactionDate", namespaces=NS))
        totals = pay.find("ns:DocumentTotals", namespaces=NS)

        if not date_s or totals is None:
            errors.append(f"Payment {ref}: incompleto (TransactionDate/DocumentTotals).")
            continue

        try:
            net = _float(totals.find("ns:NetTotal", namespaces=NS))
            tax = _float(totals.find("ns:TaxPayable", namespaces=NS))
            gross = _float(totals.find("ns:GrossTotal", namespaces=NS))
        except Exception:
            errors.append(f"Payment {ref}: totais inválidos.")
            continue

        if abs((net + tax) - gross) > 0.01:
            errors.append(f"Payment {ref}: GrossTotal incoerente.")

        # ATCUD obrigatório >= 2023
        try:
            tdate = datetime.fromisoformat(date_s).date()
        except Exception:
            errors.append(f"Payment {ref}: TransactionDate inválido.")
            continue

        atcud = _ftext(pay.find("ns:ATCUD", namespaces=NS))
        if tdate >= datetime(2023, 1, 1).date():
            if not atcud or not _ATCUD_RE.match(atcud):
                errors.append(f"Payment {ref}: ATCUD inválido ou em falta.")

        # Hash opcional em Payments: validar apenas se existir e != 0
        h = _ftext(pay.find("ns:Hash", namespaces=NS))
        if h and h != "0":
            hc = _ftext(pay.find("ns:HashControl", namespaces=NS))
            if not hc or not re.fullmatch(r"\d+(\.\d+)?", hc):
                errors.append(f"Payment {ref}: HashControl inválido.")

        for line in pay.findall("ns:Line", namespaces=NS):
            src = _ftext(line.find("ns:SourceDocumentID/ns:OriginatingON", namespaces=NS))
            if not src or src not in invoices:
                errors.append(f"Payment {ref}: referência a Invoice inexistente ({src or 'vazio'}).")

# ======================================================
# PASSO 9 — SUPPLIERS + PURCHASE INVOICES
# ======================================================

def validar_suppliers(xml_root, errors):
    suppliers = xml_root.findall("ns:MasterFiles/ns:Supplier", namespaces=NS)
    if not suppliers:
        return

    seen = set()
    for s in suppliers:
        sid = _ftext(s.find("ns:SupplierID", namespaces=NS))
        if not sid:
            errors.append("Supplier sem SupplierID.")
            continue
        if sid in seen:
            errors.append(f"SupplierID duplicado: {sid}.")
        seen.add(sid)

        name = _ftext(s.find("ns:CompanyName", namespaces=NS))
        if not name:
            errors.append(f"Supplier {sid}: CompanyName obrigatório.")

        taxid = _ftext(s.find("ns:SupplierTaxID", namespaces=NS))
        if not taxid:
            errors.append(f"Supplier {sid}: SupplierTaxID obrigatório.")

        addr = s.find("ns:BillingAddress", namespaces=NS)
        if addr is None:
            errors.append(f"Supplier {sid}: BillingAddress em falta.")
        else:
            for f in ["AddressDetail", "City", "Country"]:
                if not _ftext(addr.find(f"ns:{f}", namespaces=NS)):
                    errors.append(f"Supplier {sid}: BillingAddress.{f} obrigatório.")

            country = _ftext(addr.find("ns:Country", namespaces=NS))
            if country == "PT" and taxid and not _validar_nif_pt(taxid):
                errors.append(f"Supplier {sid}: NIF PT inválido.")

def validar_purchase_invoices(xml_root, errors):
    pi = xml_root.find("ns:SourceDocuments/ns:PurchaseInvoices", namespaces=NS)
    if pi is None:
        return

    invoices = pi.findall("ns:Invoice", namespaces=NS)
    if not invoices:
        errors.append("PurchaseInvoices sem Invoice.")
        return

    # Map suppliers
    suppliers = xml_root.findall("ns:MasterFiles/ns:Supplier", namespaces=NS)
    supplier_ids = set()
    for s in suppliers:
        sid = _ftext(s.find("ns:SupplierID", namespaces=NS))
        if sid:
            supplier_ids.add(sid)

    # Map products
    products = xml_root.findall("ns:MasterFiles/ns:Product", namespaces=NS)
    product_codes = set()
    for p in products:
        pc = _ftext(p.find("ns:ProductCode", namespaces=NS))
        if pc:
            product_codes.add(pc)

    for inv in invoices:
        inv_no = _ftext(inv.find("ns:InvoiceNo", namespaces=NS)) or "<sem InvoiceNo>"
        inv_date = _ftext(inv.find("ns:InvoiceDate", namespaces=NS))
        sid = _ftext(inv.find("ns:SupplierID", namespaces=NS))
        totals = inv.find("ns:DocumentTotals", namespaces=NS)

        if not inv_date:
            errors.append(f"PurchaseInvoice {inv_no}: InvoiceDate em falta.")
        else:
            try:
                datetime.fromisoformat(inv_date).date()
            except Exception:
                errors.append(f"PurchaseInvoice {inv_no}: InvoiceDate inválido.")

        if not sid:
            errors.append(f"PurchaseInvoice {inv_no}: SupplierID em falta.")
        else:
            # só valida existência se houver suppliers no ficheiro
            if supplier_ids and sid not in supplier_ids:
                errors.append(f"PurchaseInvoice {inv_no}: SupplierID {sid} não existe em MasterFiles.")

        if totals is None:
            errors.append(f"PurchaseInvoice {inv_no}: DocumentTotals em falta.")
        else:
            try:
                net = _float(totals.find("ns:NetTotal", namespaces=NS))
                tax = _float(totals.find("ns:TaxPayable", namespaces=NS))
                gross = _float(totals.find("ns:GrossTotal", namespaces=NS))
                if net < 0 or tax < 0 or gross < 0:
                    errors.append(f"PurchaseInvoice {inv_no}: totais negativos.")
                if abs((net + tax) - gross) > 0.01:
                    errors.append(f"PurchaseInvoice {inv_no}: GrossTotal incoerente.")
            except Exception:
                errors.append(f"PurchaseInvoice {inv_no}: totais inválidos.")

        # linhas: ProductCode deve existir; TaxCode deve existir na TaxTable (IVA/PT)
        for line in inv.findall("ns:Line", namespaces=NS):
            pc = _ftext(line.find("ns:ProductCode", namespaces=NS))
            if not pc or (product_codes and pc not in product_codes):
                errors.append(f"PurchaseInvoice {inv_no}: ProductCode inválido (não existe em MasterFiles).")

            tax = line.find("ns:Tax", namespaces=NS)
            if tax is not None:
                tax_type = _ftext(tax.find("ns:TaxType", namespaces=NS))
                region = _ftext(tax.find("ns:TaxCountryRegion", namespaces=NS))
                code = _ftext(tax.find("ns:TaxCode", namespaces=NS))
                if tax_type == "IVA" and region == "PT":
                    if code and code not in IVA_REGRAS:
                        errors.append(f"PurchaseInvoice {inv_no}: IVA TaxCode desconhecido na linha: {code}.")
