import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import io
import docx
from bson import ObjectId
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import connect_db, close_db, get_database
from app.core.security import create_access_token

@pytest.fixture(autouse=True)
async def setup_db(monkeypatch):
    await connect_db()
    from app.services import cloudinary_service
    store = {}
    async def fake_upload(data, user_id="default", file_name="resume.pdf"):
        url = f"https://res.cloudinary.test/resumes/{user_id}/{file_name}"
        store[url] = data
        return {"cloudinary_url": url, "cloudinary_public_id": f"resumes/{user_id}/{file_name}"}
    async def fake_delete(public_id):
        pass

    monkeypatch.setattr(cloudinary_service, "is_cloudinary_configured", lambda: True)
    monkeypatch.setattr(cloudinary_service, "upload_resume", fake_upload)
    monkeypatch.setattr(cloudinary_service, "delete_resume", fake_delete)
    yield
    await close_db()

def create_sample_pdf_bytes() -> bytes:
    try:
        import pymupdf
        doc = pymupdf.open()
        page = doc.new_page()
        text = (
            "John Doe\n"
            "Databricks Consultant Resume\n"
            "10+ years of experience in Databricks, PySpark, Azure, ADF, Spark, SQL, Delta Lake.\n"
            "Summary: Senior Databricks Consultant with over 10 years experience architecting lakehouses.\n"
        )
        page.insert_text((50, 50), text)
        return doc.tobytes()
    except (ImportError, Exception):
        pass

    stream_content = b"BT /F1 12 Tf 50 700 Td (John Doe - Databricks Consultant Resume) Tj 0 -20 Td (10+ years of experience in Databricks, PySpark, Azure, ADF, Spark, SQL, Delta Lake.) Tj ET"
    stream_len = len(stream_content)
    pdf = bytearray()
    pdf.extend(b"%PDF-1.4\n")
    o1 = len(pdf)
    pdf.extend(b"1 0 obj\n<</Type /Catalog /Pages 2 0 R>>\nendobj\n")
    o2 = len(pdf)
    pdf.extend(b"2 0 obj\n<</Type /Pages /Kids [3 0 R] /Count 1>>\nendobj\n")
    o3 = len(pdf)
    pdf.extend(b"3 0 obj\n<</Type /Page /Parent 2 0 R /Resources <</Font <</F1 4 0 R>>>> /MediaBox [0 0 612 792] /Contents 5 0 R>>\nendobj\n")
    o4 = len(pdf)
    pdf.extend(b"4 0 obj\n<</Type /Font /Subtype /Type1 /BaseFont /Helvetica>>\nendobj\n")
    o5 = len(pdf)
    pdf.extend(f"5 0 obj\n<</Length {stream_len}>>\nstream\n".encode("ascii"))
    pdf.extend(stream_content)
    pdf.extend(b"\nendstream\nendobj\n")
    xref_pos = len(pdf)
    pdf.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in [o1, o2, o3, o4, o5]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(f"trailer\n<</Size 6 /Root 1 0 R>>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii"))
    return bytes(pdf)

def create_sample_docx_bytes() -> bytes:
    doc = docx.Document()
    doc.add_heading("Jane Smith - AI Engineer", level=1)
    doc.add_paragraph("AI Engineer with 5 years of experience in Python, FastAPI, LangChain, RAG, Docker, Kubernetes.")
    doc.add_paragraph("Summary: Experienced AI Engineer specializing in LLM systems and production RAG.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()

@pytest.mark.anyio
async def test_resume_lifecycle():
    transport = ASGITransport(app=app)
    db = get_database()
    user_id = str(ObjectId())
    await db.users.insert_one({"_id": ObjectId(user_id), "email": "resume_tester@test.com", "name": "Resume Tester"})
    token = create_access_token(user_id)
    headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 0. Verify unauthenticated returns 401
        unauth = await client.get("/api/resumes")
        assert unauth.status_code == 401

        # 1. Upload PDF
        pdf_bytes = create_sample_pdf_bytes()
        files = [
            ("files", ("Databricks_Consultant.pdf", pdf_bytes, "application/pdf"))
        ]
        res = await client.post("/api/resumes/upload", files=files, headers=headers)
        assert res.status_code == 200, res.text
        uploaded = res.json()
        assert len(uploaded) == 1
        pdf_resume = uploaded[0]
        assert "Databricks" in pdf_resume["target_role"] or "Databricks" in pdf_resume["display_name"]
        assert "Databricks" in pdf_resume["skills"]
        assert "PySpark" in pdf_resume["skills"]
        pdf_id = pdf_resume["id"]

        # 2. Upload DOCX
        docx_bytes = create_sample_docx_bytes()
        files = [
            ("files", ("AI_Engineer_v1.docx", docx_bytes, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
        ]
        res2 = await client.post("/api/resumes/upload", files=files, headers=headers)
        assert res2.status_code == 200, res2.text
        docx_resume = res2.json()[0]
        assert "AI Engineer" in docx_resume["target_role"] or "AI Engineer" in docx_resume["display_name"]
        assert "Python" in docx_resume["skills"]
        docx_id = docx_resume["id"]

        # 3. List Resumes
        res_list = await client.get("/api/resumes", headers=headers)
        assert res_list.status_code == 200
        all_resumes = res_list.json()
        ids = [r["id"] for r in all_resumes]
        assert pdf_id in ids
        assert docx_id in ids

        # 4. Search Resumes
        search_res = await client.get("/api/resumes?search=Databricks", headers=headers)
        assert search_res.status_code == 200
        matched = search_res.json()
        assert any(r["id"] == pdf_id for r in matched)

        # 5. Get Detail
        detail_res = await client.get(f"/api/resumes/{pdf_id}", headers=headers)
        assert detail_res.status_code == 200
        detail = detail_res.json()
        assert "Databricks" in detail["raw_text"]

        # 6. Update Resume & Custom Field
        update_data = {
            "display_name": "Senior Databricks Principal",
            "custom_fields": {"hourly_rate": 120, "security_clearance": "Public Trust"}
        }
        put_res = await client.put(f"/api/resumes/{pdf_id}", json=update_data, headers=headers)
        assert put_res.status_code == 200
        updated = put_res.json()
        assert updated["display_name"] == "Senior Databricks Principal"
        assert updated["custom_fields"]["hourly_rate"] == 120

        # 6b. Replace Resume File
        replacement_pdf = create_sample_pdf_bytes()
        replace_files = [
            ("file", ("Databricks_Updated_v2.pdf", replacement_pdf, "application/pdf"))
        ]
        rep_res = await client.post(f"/api/resumes/{pdf_id}/replace", files=replace_files, headers=headers)
        assert rep_res.status_code == 200, rep_res.text
        replaced = rep_res.json()
        assert replaced["id"] == pdf_id
        assert "Databricks_Updated_v2" in replaced["file_name"]

        # 7. Delete Resumes
        del1 = await client.delete(f"/api/resumes/{pdf_id}", headers=headers)
        assert del1.status_code == 200
        assert del1.json()["success"] is True

        del2 = await client.delete(f"/api/resumes/{docx_id}", headers=headers)
        assert del2.status_code == 200
        assert del2.json()["success"] is True

        # Verify deletion
        get_deleted = await client.get(f"/api/resumes/{pdf_id}", headers=headers)
        assert get_deleted.status_code == 404
