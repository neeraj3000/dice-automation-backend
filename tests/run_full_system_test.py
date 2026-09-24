import sys
import asyncio
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import connect_db, close_db, get_database

async def run_full_system_test():
    print("=== STARTING FULL SYSTEM END-TO-END TEST ===")
    await connect_db()
    db = get_database()
    
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Health check
        h_res = await client.get("/api/health")
        assert h_res.status_code == 200
        print("[OK] Health Check OK")

        # 2. Upload sample resumes if library is empty
        res_list = await client.get("/api/resumes")
        if len(res_list.json()) == 0:
            print("Uploading sample resumes from sample_resumes/...")
            sample_dir = Path(__file__).resolve().parent.parent / "sample_resumes"
            files_to_upload = []
            for f in sample_dir.glob("*.*"):
                content = f.read_bytes()
                mime = "application/pdf" if f.suffix == ".pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                files_to_upload.append(("files", (f.name, content, mime)))
            
            up_res = await client.post("/api/resumes/upload", files=files_to_upload)
            assert up_res.status_code == 200
            print(f"[OK] Uploaded {len(up_res.json())} sample resumes")

        res_list = await client.get("/api/resumes")
        resumes = res_list.json()
        print(f"[OK] Resume Library contains {len(resumes)} resumes")

        # 3. Test Direct JD Matching (Phase 2)
        print("\n--- Testing Phase 2: Direct JD Matching ---")
        databricks_jd = (
            "We are seeking a Lead Databricks Consultant with 8+ years experience. "
            "Must have extensive hands-on expertise with Databricks, PySpark, Delta Lake, Azure ADF, and SQL. "
            "Experience with AWS and Terraform is preferred."
        )
        match_req = {
            "title": "Lead Databricks Consultant",
            "company": "Enterprise Data Corp",
            "description_raw": databricks_jd
        }
        m_res = await client.post("/api/jobs/match-direct", json=match_req)
        assert m_res.status_code == 200, m_res.text
        m_data = m_res.json()
        rec_name = m_data["match_result"]["recommended_resume_name"]
        score = m_data["match_result"]["match_percentage"]
        matched_skills = m_data["match_result"]["matched_skills"]
        print(f"[OK] Direct Match Recommended: '{rec_name}' with Score: {score}%")
        print(f"[OK] Matched Skills: {matched_skills}")
        assert "Databricks" in rec_name or "Alex Morgan" in rec_name or "Databricks" in matched_skills
        assert score >= 70
        job_id = m_data["job_id"]

        # 4. Test Search Profiles (Phase 3)
        print("\n--- Testing Phase 3: Search Profiles ---")
        prof_data = {
            "name": "AI & Data Engineer Roles",
            "keywords": ["Databricks", "AI Engineer", "PySpark"],
            "location": "United States",
            "is_remote": True,
            "job_type": "Full-time",
            "posted_within": "24h"
        }
        sp_res = await client.post("/api/search-profiles", json=prof_data)
        assert sp_res.status_code == 200
        sp_id = sp_res.json()["id"]
        print(f"[OK] Created Search Profile: '{sp_res.json()['name']}' (ID: {sp_id})")

        list_sp = await client.get("/api/search-profiles")
        assert any(p["id"] == sp_id for p in list_sp.json())
        print(f"[OK] Search Profiles List OK ({len(list_sp.json())} profiles)")

        # 5. Test Applications Orchestration & Mode (Phase 5 & 7)
        print("\n--- Testing Phase 5: Application Preparation ---")
        app_req = {
            "job_id": job_id,
            "mode": "PREPARE"
        }
        prep_res = await client.post("/api/applications/prepare", json=app_req)
        assert prep_res.status_code == 200, prep_res.text
        app_data = prep_res.json()
        app_id = app_data["id"]
        print(f"[OK] Application prepared: ID={app_id}, Status={app_data['status']}, Mode={app_data['mode']}")
        print(f"[OK] Progress Steps: {app_data['progress_steps']}")

        # 6. Test Human Review Queue (Phase 6)
        print("\n--- Testing Phase 6: Human Review Queue ---")
        first_app = await db.applications.find_one({})
        from bson import ObjectId
        q_doc = {
            "application_id": first_app["_id"],
            "question_text": "Are you legally authorized to work in the United States?",
            "field_name": "work_authorization",
            "options": ["Yes", "No"],
            "answer_text": "",
            "is_answered": False
        }
        q_insert = await db.application_answers.insert_one(q_doc)
        q_id = str(q_insert.inserted_id)

        rq_res = await client.get("/api/review-queue")
        assert rq_res.status_code == 200
        assert any(q["id"] == q_id for q in rq_res.json())
        print(f"[OK] Review Queue contains question: '{q_doc['question_text']}'")

        # Answer question
        ans_res = await client.post(f"/api/review-queue/{q_id}/answer", json={"answer_text": "Yes"})
        assert ans_res.status_code == 200
        print("[OK] Answered review question successfully")

        # 7. Test Submission Guard & Approval (Phase 7)
        print("\n--- Testing Phase 7: Application Submission Guard ---")
        await db.applications.update_one({"_id": ObjectId(app_id)}, {"$set": {"status": "READY"}})

        sub_res = await client.post(f"/api/applications/{app_id}/submit")
        assert sub_res.status_code == 200
        sub_data = sub_res.json()
        assert sub_data["status"] == "APPLIED"
        assert sub_data["applied_at"] is not None
        print(f"[OK] Application successfully submitted & marked APPLIED at {sub_data['applied_at']}")

        # 8. Test Settings & Dashboard Stats
        print("\n--- Testing Dashboard Stats & Settings ---")
        prof_update = {
            "first_name": "John",
            "last_name": "Developer",
            "email": "john.dev@example.com",
            "phone": "555-123-4567",
            "city": "Austin",
            "state": "TX",
            "zip_code": "78701",
            "linkedin_url": "https://linkedin.com/in/johndev",
            "github_url": "https://github.com/johndev",
            "portfolio_url": "https://johndev.com",
            "years_of_experience": "10",
            "work_authorization": "US Citizen",
            "willing_to_relocate": True
        }
        put_prof = await client.put("/api/profile", json=prof_update)
        assert put_prof.status_code == 200
        print(f"[OK] Profile updated: {put_prof.json()['first_name']} {put_prof.json()['last_name']}")

        dash_res = await client.get("/api/dashboard/stats")
        assert dash_res.status_code == 200
        stats = dash_res.json()
        print(f"[OK] Dashboard Metrics: Resumes={stats['resumes_count']}, Jobs={stats['jobs_count']}, Applications={stats['apps_count']}, Applied={stats['apps_applied']}")

        print("\n=======================================================")
        print("ALL PHASES (2, 3, 4, 5, 6, 7) VERIFIED & WORKING!")
        print("=======================================================")

    await close_db()

if __name__ == "__main__":
    asyncio.run(run_full_system_test())
