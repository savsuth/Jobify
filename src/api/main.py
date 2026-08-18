from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy.orm import Session

from src.db.models import Job, JobAnalysis
from src.db.session import get_session

app = FastAPI(title="Job Application Agent API")


@app.get("/jobs")
def list_jobs(
    status: str | None = Query(default=None, pattern="^(pending|rejected|passed|ineligible)$"),
    session: Session = Depends(get_session),
):
    query = session.query(Job, JobAnalysis).outerjoin(JobAnalysis, JobAnalysis.job_id == Job.id)
    if status is not None:
        query = query.filter(JobAnalysis.status == status)

    results = []
    for job, analysis in query.order_by(Job.discovered_at.desc()).all():
        results.append(
            {
                "id": job.id,
                "source": job.source,
                "company": job.company,
                "title": job.title,
                "location": job.location,
                "remote_type": job.remote_type,
                "employment_type": job.employment_type,
                "location_category": job.location_category,
                "seniority": job.seniority,
                "work_auth_status": job.work_auth_status,
                "work_auth_reason": job.work_auth_reason,
                "url": job.url,
                "posted_at": job.posted_at,
                "discovered_at": job.discovered_at,
                "match_score": analysis.match_score if analysis else None,
                "status": analysis.status if analysis else "pending",
            }
        )
    return results


@app.get("/jobs/{job_id}")
def get_job(job_id: int, session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    analysis = session.query(JobAnalysis).filter(JobAnalysis.job_id == job_id).first()
    return {
        "id": job.id,
        "source": job.source,
        "company": job.company,
        "title": job.title,
        "location": job.location,
        "remote_type": job.remote_type,
        "employment_type": job.employment_type,
        "location_category": job.location_category,
        "seniority": job.seniority,
        "work_auth_status": job.work_auth_status,
        "work_auth_reason": job.work_auth_reason,
        "url": job.url,
        "description_raw": job.description_raw,
        "posted_at": job.posted_at,
        "discovered_at": job.discovered_at,
        "analysis": (
            {
                "match_score": analysis.match_score,
                "hard_requirements_met": analysis.hard_requirements_met,
                "hard_requirements_missing": analysis.hard_requirements_missing,
                "matched_skills": analysis.matched_skills,
                "missing_skills": analysis.missing_skills,
                "reasoning": analysis.reasoning,
                "status": analysis.status,
            }
            if analysis
            else None
        ),
    }
