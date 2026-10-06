from fastapi import APIRouter

from app.api.v1 import (
    accounting,
    auth,
    bidding,
    catalog,
    communities,
    compliance,
    cost_codes,
    crm,
    design_studio,
    documents,
    drawings,
    field,
    health,
    intake,
    jobs,
    labour,
    land,
    marketing,
    meetings,
    operations,
    options,
    plans,
    portal,
    purchasing,
    reporting,
    rfi,
    safety,
    schedule,
    submittal,
    survey,
    users,
    vendors,
    warranty,
)

api_router = APIRouter()

# Platform
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)

# Configuration
# Before catalog: its /cost-codes/{code_id} would otherwise swallow /cost-codes/stats.
api_router.include_router(cost_codes.router)
api_router.include_router(catalog.router)
api_router.include_router(communities.router)
api_router.include_router(plans.router)
api_router.include_router(options.router)
api_router.include_router(vendors.router)

# Sales and marketing
api_router.include_router(crm.router)
api_router.include_router(intake.router)
api_router.include_router(design_studio.router)
api_router.include_router(marketing.router)
api_router.include_router(schedule.router)
api_router.include_router(field.router)

# Job lifecycle
api_router.include_router(jobs.router)
api_router.include_router(purchasing.router)
api_router.include_router(operations.router)
api_router.include_router(documents.router)
api_router.include_router(warranty.router)

api_router.include_router(bidding.router)
# Land development (the last of the old "extended" catch-all)
api_router.include_router(land.router)
api_router.include_router(land.items_router)

# Prequalification, RFIs, submittals, drawings, safety, meetings, timecards
api_router.include_router(labour.router)
api_router.include_router(safety.router)
api_router.include_router(compliance.router)
api_router.include_router(rfi.router)
api_router.include_router(submittal.router)
api_router.include_router(drawings.router)
api_router.include_router(meetings.router)

# Accounting export and the homeowner portal
api_router.include_router(accounting.router)

# External and analytics
api_router.include_router(survey.router)
api_router.include_router(portal.router)
api_router.include_router(reporting.router)
