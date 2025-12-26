from flask import Flask, request, render_template, jsonify
from lxml import etree
import io, re, base64
from datetime import datetime

import pytesseract
from pdf2image import convert_from_bytes

# ==========================
# SAF-T XML (MANTER ESTÁVEL)
# ==========================
from validators.fiscal import (
    validar_header,
    validar_datas,
    validar_nif,
    validar_tax_table,
    validar_sales_invoices,
    validar_atcud_hash_sales_invoices,
    validar_customers,
    validar_products,
    validar_payments,
)

app = Flask(__name__)

XSD_FILE = "/app/saftpt_base_lxml.xsd"
SAFT_NS = {"saft": "urn:OECD:StandardAuditFile-Tax:PT_1.04_01"}

CHECKS_XML = [
    "BASE",
    "Fiscal",
    "IVA",
    "SourceDocuments",
    "ATCUD/Hash",
    "Customers",
    "Products",
    "Payments",
]

CHECKS_PDF = [
    "OCR",
    "Documento",
    "Identificação",
    "Totais",
    "Sinais fiscais (best-effort)",
]


# ==========================
# Util
# ==========================
def wants_json():
    return (
        request.headers.get("Accept") == "application/json"
        or request.headers.get("X-CI-Mode") == "true"
    )


def _lines(text_raw: str):
    return [ln.strip() for ln in (text_raw or "").splitlines() if ln.strip()]


def _norm(text_raw: str):
    return " ".join((text_raw or "").split())


def _field(label, value, status="ok", note=""):
    return {
        "label": label,
        "value": value if value not in [None, ""] else "—",
        "status": status,
        "note": note or "",
    }


def _validar_nif_pt(nif: str) -> bool:
    if not re.fullmatch(r"\d{9}", nif or ""):
        return False
    if nif[0] not in "1235689":
        return False
    total = sum(int(nif[i]) * (9 - i) for i in range(8))
    check = 11 - (total % 11)
    if check >= 10:
        check = 0
    return check == int(nif[8])


def _unique(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _parse_money(s: str):
    if not s:
        return None
    v = s.strip()
    v = v.replace("€", "").replace("EUR", "")
    v = v.replace(" ", "")
    if "," in v and "." in v:
        v = v.replace(".", "")
    v = v.replace(",", ".")
    try:
        return float(v)
    except Exception:
        return None


def _money_re():
    return r"([0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2}|[0-9]+,[0-9]{2}|[0-9]+\.[0-9]{2})"


def _find_first(patterns, text, flags=re.I):
    for p in patterns:
        m = re.search(p, text, flags)
        if m:
            return m.group(1) if m.groups() else m.group(0)
    return None


# ==========================
# SAF-T invoice renderer (NOVO, só leitura)
# ==========================
def _xtext(node, xpath, default="—"):
    if node is None:
        return default
    el = node.find(xpath, namespaces=SAFT_NS)
    if el is None or el.text is None:
        return default
    t = el.text.strip()
    return t if t else default


def _find(node, xpath):
    if node is None:
        return None
    return node.find(xpath, namespaces=SAFT_NS)


def _findall(node, xpath):
    if node is None:
        return []
    return node.findall(xpath, namespaces=SAFT_NS)


def extract_invoice_view(tree):
    root = tree.getroot()

    header = _find(root, "saft:Header")
    master = _find(root, "saft:MasterFiles")
    src = _find(root, "saft:SourceDocuments")

    # Primeira fatura de vendas
    invoice = _find(src, "saft:SalesInvoices/saft:Invoice")
    if invoice is None:
        return None

    customer_id = _xtext(invoice, "saft:CustomerID", default=None)

    # Customer details (por CustomerID)
    customer = None
    if master is not None and customer_id:
        customers = _findall(master, "saft:Customer")
        for c in customers:
            if _xtext(c, "saft:CustomerID", default="") == customer_id:
                customer = c
                break

    # Supplier from Header
    supplier = {
        "company_name": _xtext(header, "saft:CompanyName"),
        "nif": _xtext(header, "saft:TaxRegistrationNumber"),
        "company_id": _xtext(header, "saft:CompanyID"),
        "email": _xtext(header, "saft:Email"),
        "software_cert": _xtext(header, "saft:SoftwareCertificateNumber"),
        "product_id": _xtext(header, "saft:ProductID"),
        "product_version": _xtext(header, "saft:ProductVersion"),
        "address_detail": _xtext(header, "saft:CompanyAddress/saft:AddressDetail"),
        "city": _xtext(header, "saft:CompanyAddress/saft:City"),
        "postal": _xtext(header, "saft:CompanyAddress/saft:PostalCode"),
        "country": _xtext(header, "saft:CompanyAddress/saft:Country"),
        "currency": _xtext(header, "saft:CurrencyCode"),
    }

    cust = {
        "customer_id": _xtext(customer, "saft:CustomerID"),
        "company_name": _xtext(customer, "saft:CompanyName"),
        "nif": _xtext(customer, "saft:CustomerTaxID"),
        "contact": _xtext(customer, "saft:Contact"),
        "email": _xtext(customer, "saft:Email"),
        "addr_detail": _xtext(customer, "saft:BillingAddress/saft:AddressDetail"),
        "city": _xtext(customer, "saft:BillingAddress/saft:City"),
        "postal": _xtext(customer, "saft:BillingAddress/saft:PostalCode"),
        "country": _xtext(customer, "saft:BillingAddress/saft:Country"),
    }

    inv = {
        "invoice_no": _xtext(invoice, "saft:InvoiceNo"),
        "invoice_date": _xtext(invoice, "saft:InvoiceDate"),
        "invoice_type": _xtext(invoice, "saft:InvoiceType"),
        "period": _xtext(invoice, "saft:Period"),
        "system_entry_date": _xtext(invoice, "saft:SystemEntryDate"),
        "atcud": _xtext(invoice, "saft:ATCUD"),
        "hash": _xtext(invoice, "saft:Hash"),
        "hash_control": _xtext(invoice, "saft:HashControl"),
        "source_id": _xtext(invoice, "saft:SourceID"),
        "customer_id": _xtext(invoice, "saft:CustomerID"),
    }

    # Totals
    totals = _find(invoice, "saft:DocumentTotals")
    doc_totals = {
        "net": _xtext(totals, "saft:NetTotal"),
        "tax": _xtext(totals, "saft:TaxPayable"),
        "gross": _xtext(totals, "saft:GrossTotal"),
        "currency": supplier.get("currency", "—"),
    }

    # Lines
    lines = []
    for ln in _findall(invoice, "saft:Line"):
        tax = _find(ln, "saft:Tax")
        line = {
            "line_number": _xtext(ln, "saft:LineNumber"),
            "product_code": _xtext(ln, "saft:ProductCode"),
            "product_desc": _xtext(ln, "saft:ProductDescription"),
            "description": _xtext(ln, "saft:Description"),
            "qty": _xtext(ln, "saft:Quantity"),
            "uom": _xtext(ln, "saft:UnitOfMeasure"),
            "unit_price": _xtext(ln, "saft:UnitPrice"),
            "credit_amount": _xtext(ln, "saft:CreditAmount"),
            "debit_amount": _xtext(ln, "saft:DebitAmount"),
            "tax_type": _xtext(tax, "saft:TaxType"),
            "tax_code": _xtext(tax, "saft:TaxCode"),
            "tax_pct": _xtext(tax, "saft:TaxPercentage"),
            "exemption_code": _xtext(ln, "saft:TaxExemptionCode"),
            "exemption_reason": _xtext(ln, "saft:TaxExemptionReason"),
        }
        lines.append(line)

    # Shipping (opcional)
    ship_to = _find(invoice, "saft:ShipTo/saft:Address")
    ship_from = _find(invoice, "saft:ShipFrom/saft:Address")
    shipping = {
        "to": {
            "addr": _xtext(ship_to, "saft:AddressDetail"),
            "city": _xtext(ship_to, "saft:City"),
            "postal": _xtext(ship_to, "saft:PostalCode"),
            "country": _xtext(ship_to, "saft:Country"),
        },
        "from": {
            "addr": _xtext(ship_from, "saft:AddressDetail"),
            "city": _xtext(ship_from, "saft:City"),
            "postal": _xtext(ship_from, "saft:PostalCode"),
            "country": _xtext(ship_from, "saft:Country"),
        },
    }

    return {
        "supplier": supplier,
        "customer": cust,
        "invoice": inv,
        "lines": lines,
        "totals": doc_totals,
        "shipping": shipping,
    }


# ==========================
# Route: SAF-T XML (ESTÁVEL)
# ==========================
@app.route("/", methods=["GET", "POST"])
def index():
    errors = []
    valid = False
    summary = None
    status = 200
    invoice_view = None

    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename.lower().endswith(".xml"):
            errors.append("Ficheiro XML inválido.")
            status = 400
        else:
            try:
                xml = etree.parse(
                    io.BytesIO(f.read()),
                    etree.XMLParser(huge_tree=True, no_network=True, resolve_entities=False),
                )

                # XSD base (mantém estável)
                with open(XSD_FILE, "rb") as x:
                    schema = etree.XMLSchema(etree.XML(x.read()))

                if not schema.validate(xml):
                    for e in schema.error_log:
                        errors.append(f"Linha {e.line}: {e.message}")
                    status = 422
                else:
                    r = xml.getroot()

                    # Validações fiscais (mantém estável)
                    validar_header(r, errors)
                    validar_datas(r, errors)
                    validar_nif(r, errors)
                    validar_tax_table(r, errors)
                    validar_sales_invoices(r, errors)
                    validar_atcud_hash_sales_invoices(r, errors)
                    validar_customers(r, errors)
                    validar_products(r, errors)
                    validar_payments(r, errors)

                    if not errors:
                        valid = True
                        summary = "SAF-T válido (BASE + Fiscal + IVA + SourceDocuments + ATCUD/Hash + Customers + Products + Payments)"
                        # NOVO: apenas renderização, não interfere na validação
                        invoice_view = extract_invoice_view(xml)
                    else:
                        status = 422

            except Exception as e:
                errors.append(str(e))
                status = 500

    if wants_json():
        return (
            jsonify(
                {
                    "valid": valid,
                    "summary": summary,
                    "errors": errors,
                    "checks": CHECKS_XML,
                    "invoice_view": invoice_view,
                }
            ),
            status,
        )

    return render_template(
        "index.html",
        result=summary if valid else None,
        errors=errors,
        invoice_view=invoice_view,
    )


# ==========================
# PDF Purchase (ESTÁVEL)
# ==========================
def _doc_type(text_norm: str):
    low = (text_norm or "").lower()
    if "não serve de fatura" in low or "nao serve de fatura" in low:
        return ("NAO_FATURA", "⚠ Este documento não serve de fatura (proposta/orçamento).")
    if "fatura" in low or "factura" in low:
        return ("FATURA", None)
    return ("DESCONHECIDO", "⚠ Tipo de documento não identificado com confiança.")


def _extract_nifs(text_norm: str):
    nifs = re.findall(r"(?:PT[\s\-:]*)?(\d{9})", text_norm)
    return _unique(nifs)


def _guess_customer_nif(lines, nifs_validos):
    for ln in lines:
        low = ln.lower()
        if "contribu" in low or "contrib." in low or "contrib:" in low:
            for n in nifs_validos:
                if n in ln:
                    return n
    return nifs_validos[0] if nifs_validos else None


def _guess_supplier_nif(nifs_validos, customer_nif):
    for n in nifs_validos:
        if customer_nif and n != customer_nif:
            return n
    return None


def _guess_customer_name(lines):
    for ln in lines:
        if re.search(r"\bCIA\s+LINUX\b", ln, re.I):
            return ln.strip()
        if re.search(r"\bCia\s+Linux\b", ln, re.I):
            return ln.strip()
    for i, ln in enumerate(lines):
        if re.search(r"Exmos\.\s*Senhores", ln, re.I) and i > 0:
            for j in range(max(0, i - 3), i):
                cand = lines[j].strip()
                if len(cand) >= 4 and re.search(r"Lda|LDA|Unipessoal|S\.A\.", cand, re.I):
                    return cand
    return None


def _guess_supplier_name(lines):
    for ln in lines[:40]:
        if re.search(r"\bLDA\b|\bLda\b|S\.A\.", ln):
            if re.search(r"\bCIA\s+LINUX\b", ln, re.I):
                continue
            if "marques" in ln.lower() or "santogal" in ln.lower():
                return ln.strip().strip(".")
    for ln in lines[-50:]:
        if re.search(r"\bLDA\b|\bLda\b|S\.A\.", ln):
            if re.search(r"santogal|marques", ln, re.I):
                return ln.strip().strip(".")
    return None


def _extract_atcud(text_norm: str):
    m = re.search(r"\b([A-Z0-9]{8,}-\d{1,})\b", text_norm)
    return m.group(1) if m else None


def _extract_invoice_no(text_norm: str):
    return _find_first(
        [
            r"\b(NFT\s+FTA/\d+)\b",
            r"\b([A-Z]{1,4}/\d{3,})\b",
            r"\b(FT\s+\d+/\d+)\b",
            r"\b(FA\s+\d+/\d+)\b",
            r"\b(FR\s+\d+/\d+)\b",
            r"\b(FS\s+\d+/\d+)\b",
            r"\b(NC\s+\d+/\d+)\b",
            r"\b(ND\s+\d+/\d+)\b",
        ],
        text_norm,
    )


def _extract_date(text_norm: str, lines):
    for ln in lines[:60]:
        m = re.search(r"\bData:\s*([0-3]\d/[01]\d/\d{4})\b", ln, re.I)
        if m:
            return m.group(1)
    m = re.search(r"\b([0-3]\d/[01]\d/\d{4})\b", text_norm)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{4}-[01]\d-[0-3]\d)\b", text_norm)
    return m.group(1) if m else None


def _extract_totals(lines, text_norm):
    money = _money_re()
    total = None
    base = None
    iva = None
    taxa = None

    for ln in lines:
        if re.search(r"\bTOTAL\b", ln):
            m = re.search(r"\bTOTAL\b.*?" + money, ln, re.I)
            if m:
                total = m.group(1)

    if not total:
        m = re.search(r"TOTAL\s*\(Euros\)\s*" + money, text_norm, re.I)
        if m:
            total = m.group(1)

    for ln in lines:
        m = re.search(r"\bValor\s*Base\s*:\s*" + money, ln, re.I)
        if m:
            base = m.group(1)
        m = re.search(r"\bIVA\s*:\s*" + money, ln, re.I)
        if m:
            iva = m.group(1)

    for i, ln in enumerate(lines):
        if re.search(r"Incid[êe]ncia.*Valor\s*IVA", ln, re.I) or re.search(r"Incid[êe]ncia.*Taxa", ln, re.I):
            for j in range(i + 1, min(i + 6, len(lines))):
                m = re.search(r"^\s*" + money + r"\s+" + money + r"\s+" + money, lines[j])
                if m:
                    base = base or m.group(1)
                    taxa = taxa or m.group(2)
                    iva = iva or m.group(3)
                    break

    if not iva:
        m = re.search(r"\bIVA\s*(?:23%|23)\b.*?" + money, text_norm, re.I)
        if m:
            iva = m.group(1)

    return {"base": base, "iva": iva, "total": total, "taxa": taxa}


def _detect_program_cert(text_norm: str):
    return bool(re.search(r"programa\s+certificado\s+n[ºo]\.?\s*\d+\/AT", text_norm, re.I))


def _detect_qr_hint(text_norm: str):
    return bool(re.search(r"\bQR\b|QRCode|QR\-Code", text_norm, re.I))


@app.route("/purchase-pdf", methods=["GET", "POST"])
def purchase_pdf():
    errors = []
    extracted = {}
    invoice = None
    valid = False
    summary = None
    status_code = 200
    ocr_text_raw = None
    pdf_preview = None

    if request.method == "POST":
        file = request.files.get("file")

        if not file or not file.filename.lower().endswith(".pdf"):
            errors.append("Ficheiro PDF inválido.")
            status_code = 400
        else:
            try:
                pdf_bytes = file.read()

                images = convert_from_bytes(pdf_bytes, dpi=300, first_page=1, last_page=1)
                if not images:
                    errors.append("Não foi possível converter o PDF em imagem.")
                    status_code = 422
                else:
                    img = images[0]

                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    pdf_preview = base64.b64encode(buf.getvalue()).decode()

                    ocr_text_raw = pytesseract.image_to_string(img, lang="por")
                    lines = _lines(ocr_text_raw)
                    text_norm = _norm(ocr_text_raw)

                    extracted["ocr_chars"] = str(len(ocr_text_raw))
                    extracted["ocr_sample"] = text_norm[:220]

                    if len(ocr_text_raw.strip()) < 30:
                        errors.append("OCR não conseguiu ler texto suficiente do PDF (scan fraco).")
                        status_code = 422

                    doc_type, doc_warn = _doc_type(text_norm)
                    if doc_warn:
                        errors.append(doc_warn)

                    nifs = _extract_nifs(text_norm)
                    nifs_validos = [n for n in nifs if _validar_nif_pt(n)]

                    cliente_nif = _guess_customer_nif(lines, nifs_validos)
                    fornecedor_nif = _guess_supplier_nif(nifs_validos, cliente_nif)

                    cliente_nome = _guess_customer_name(lines)
                    fornecedor_nome = _guess_supplier_name(lines)

                    atcud = _extract_atcud(text_norm)
                    invno = _extract_invoice_no(text_norm)
                    date = _extract_date(text_norm, lines)

                    totals = _extract_totals(lines, text_norm)
                    base_s = totals.get("base")
                    iva_s = totals.get("iva")
                    total_s = totals.get("total")
                    taxa_s = totals.get("taxa")

                    inferred_note = None
                    if (not base_s or base_s == "—") and iva_s and total_s:
                        iv = _parse_money(iva_s)
                        tv = _parse_money(total_s)
                        if iv is not None and tv is not None and tv >= iv:
                            base_calc = round(tv - iv, 2)
                            base_s = f"{base_calc:.2f}".replace(".", ",")
                            inferred_note = "Base inferida por Total - IVA (não extraída diretamente)."

                    sw_cert = _detect_program_cert(text_norm)
                    qr_hint = _detect_qr_hint(text_norm)

                    invoice = {
                        "doc_type": _field("Tipo de documento", doc_type, "warn" if doc_type != "FATURA" else "ok"),
                        "numero": _field("Nº documento", invno, "ok" if invno else "warn"),
                        "data": _field("Data", date, "ok" if date else "warn"),
                        "atcud": _field("ATCUD", atcud, "ok" if atcud else "warn"),
                        "fornecedor_nome": _field("Fornecedor (nome)", fornecedor_nome, "ok" if fornecedor_nome else "warn"),
                        "fornecedor_nif": _field("Fornecedor (NIF)", fornecedor_nif, "ok" if fornecedor_nif else "warn"),
                        "cliente_nome": _field("Cliente (nome)", cliente_nome, "ok" if cliente_nome else "warn"),
                        "cliente_nif": _field("Cliente (NIF)", cliente_nif, "ok" if cliente_nif else "warn"),
                        "taxa": _field("Taxa IVA (se detetada)", taxa_s, "ok" if taxa_s else "warn"),
                        "base": _field("Base tributável / Valor Base", base_s, "inferido" if inferred_note else ("ok" if base_s else "warn"), inferred_note),
                        "iva": _field("IVA", iva_s, "ok" if iva_s else "warn"),
                        "total": _field("Total", total_s, "ok" if total_s else "warn"),
                        "software_cert": _field("Programa certificado AT", "Detetado" if sw_cert else "Inconclusivo", "ok" if sw_cert else "warn"),
                        "qr": _field("QR Code", "Detetado (best-effort)" if qr_hint else "Inconclusivo", "ok" if qr_hint else "warn"),
                        "nifs_detectados": _field("NIFs detetados (OCR)", ", ".join(nifs) if nifs else None, "ok" if nifs else "warn"),
                        "nifs_validos": _field("NIFs PT válidos", ", ".join(nifs_validos) if nifs_validos else None, "ok" if nifs_validos else "warn"),
                    }

                    b = _parse_money(base_s) if base_s else None
                    i = _parse_money(iva_s) if iva_s else None
                    t = _parse_money(total_s) if total_s else None
                    if b is not None and i is not None and t is not None:
                        if abs((b + i) - t) > 0.05:
                            errors.append("Totais incoerentes: Base + IVA ≠ Total (ou OCR leu mal).")
                            status_code = 422
                    else:
                        errors.append("Totais/IVA não foram extraídos com confiança (layout/scan).")
                        status_code = 422

                    if date and "/" in date:
                        try:
                            dd, mm, yyyy = date.split("/")
                            datetime(int(yyyy), int(mm), int(dd))
                        except Exception:
                            errors.append("Data inválida (OCR pode ter lido mal).")
                            status_code = 422

                    if not errors:
                        valid = True
                        summary = "Documento analisado com sucesso (OCR assistido)."
                    else:
                        if status_code == 200:
                            status_code = 422

            except Exception as e:
                errors.append(str(e))
                status_code = 500

    if wants_json():
        return (
            jsonify(
                {
                    "valid": valid,
                    "summary": summary,
                    "errors": errors,
                    "checks": CHECKS_PDF,
                    "extracted": extracted,
                    "invoice": invoice,
                }
            ),
            status_code,
        )

    return (
        render_template(
            "purchase_pdf.html",
            invoice=invoice,
            pdf_preview=pdf_preview,
            ocr_text=ocr_text_raw,
            errors=errors,
        ),
        status_code,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
