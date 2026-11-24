import os
import tempfile
from datetime import datetime, timezone
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from data import load_data
from config import WEEKLY_QUOTA, REIMBURSEMENT


async def generate_report():
    data = load_data()
    runners = data["runners"]
    now = datetime.now(timezone.utc)

    try:
        doc = Document()

        # ── Title ──
        title = doc.add_heading("Resilience — Runner Report", level=1)
        title.runs[0].font.color.rgb = RGBColor(0x2B, 0x2D, 0x31)

        sub = doc.add_paragraph(f"Generated: {now.strftime('%B %d, %Y at %H:%M UTC')}")
        sub.runs[0].font.size = Pt(10)
        sub.runs[0].font.color.rgb = RGBColor(0x66, 0x66, 0x66)

        doc.add_paragraph()

        # ── Runner Summary ──
        doc.add_heading("Runner Summary", level=2).runs[0].font.color.rgb = RGBColor(0xC9, 0xA8, 0x4C)

        summary_table = doc.add_table(rows=1, cols=6)
        summary_table.style = "Table Grid"
        hdr = summary_table.rows[0].cells
        for i, label in enumerate(["Runner", "Torn ID", "Total Xanax", "Credits", "Access", "Expires"]):
            hdr[i].text = label
            hdr[i].paragraphs[0].runs[0].bold = True

        for r in runners.values():
            row = summary_table.add_row().cells
            row[0].text = r.get("torn_name") or "Unknown"
            row[1].text = str(r.get("torn_id") or "—")
            row[2].text = str(r.get("total_xanax", 0))
            row[3].text = str(r.get("credits", 0))
            row[4].text = "Active" if r.get("armory_access") else "Inactive"
            expiry = r.get("access_expires")
            if expiry:
                exp_dt = datetime.fromisoformat(expiry).replace(tzinfo=timezone.utc)
                row[5].text = exp_dt.strftime("%b %d, %Y")
            else:
                row[5].text = "—"

        doc.add_paragraph()

        # ── Submission History ──
        doc.add_heading("Full Submission History", level=2).runs[0].font.color.rgb = RGBColor(0xC9, 0xA8, 0x4C)

        hist_table = doc.add_table(rows=1, cols=6)
        hist_table.style = "Table Grid"
        hdr2 = hist_table.rows[0].cells
        for i, label in enumerate(["Runner", "Date", "Xanax", "Status", "Reimbursed", "Approved By"]):
            hdr2[i].text = label
            hdr2[i].paragraphs[0].runs[0].bold = True

        for r in runners.values():
            for s in r.get("submissions", []):
                row = hist_table.add_row().cells
                row[0].text = r.get("torn_name") or "Unknown"
                try:
                    dt = datetime.fromisoformat(s["submitted_at"]).replace(tzinfo=timezone.utc)
                    row[1].text = dt.strftime("%b %d, %Y")
                except Exception:
                    row[1].text = "—"
                row[2].text = str(s.get("xanax_deposited", 0))
                row[3].text = s.get("status", "—").capitalize()
                row[4].text = "Yes" if s.get("reimbursed") else "No"
                row[5].text = s.get("approved_by") or "—"

        tmp = tempfile.mktemp(suffix=".docx")
        doc.save(tmp)
        return tmp

    except Exception as e:
        print("Report exception:", e)
        return None
