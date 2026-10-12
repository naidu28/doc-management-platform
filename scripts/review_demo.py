"""Isolated local demo; real PostgreSQL API and WebSockets, in-process write/index loop.

Run from the repository root: .venv/bin/python -m scripts.review_demo
Production continues to use redlining.main plus redlining.runtime workers.
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import uvicorn
from redlining.main import build_app
from redlining.postgres import PostgresStore

store = PostgresStore(initialize=True)
fixtures = {
 'services-agreement': ('Master Services Agreement', '''MASTER SERVICES AGREEMENT

This Master Services Agreement (the “Agreement”) is entered into as of October 12, 2026, by and between Alder Technologies, Inc. (“Client”) and Northstar Advisory, LLC (“Provider”).

01  SERVICES & SCOPE

Provider will perform the professional services described in each statement of work executed by the parties. Each statement of work will identify the deliverables, schedule, fees, and designated representatives responsible for the engagement.

Any material change to the scope of services requires the prior written agreement of both parties. Provider will promptly notify Client of any circumstances that may affect the delivery schedule.

02  PAYMENT TERMS

Client shall pay all undisputed invoices within thirty (30) days of receipt. Each invoice must include a description of the services performed and any expenses approved in advance by Client.

Reasonable travel expenses will be reimbursed at cost, provided that Provider obtains written approval before incurring them. The parties will cooperate in good faith to resolve disputed charges.

03  CONFIDENTIALITY

Each party agrees to protect the other party’s confidential information using reasonable care and to use that information solely to fulfill its obligations under this Agreement. These obligations survive termination for a period of three (3) years.

Confidential information does not include information that is publicly available, independently developed, or lawfully received from a third party without a duty of confidentiality.

04  TERM & TERMINATION

This Agreement will remain in effect for an initial term of twelve (12) months. Either party may terminate this Agreement for convenience upon thirty (30) days’ prior written notice to the other party.

Upon termination, Provider will deliver all completed work and reasonably assist with the transition of services. Client will pay for services properly performed through the effective termination date.

05  LIMITATION OF LIABILITY

Except for a party’s confidentiality obligations or willful misconduct, neither party will be liable for indirect, incidental, or consequential damages. Each party’s aggregate liability will not exceed the fees paid under this Agreement during the preceding twelve (12) months.

06  GENERAL PROVISIONS

This Agreement constitutes the entire agreement between the parties concerning its subject matter. Amendments must be in writing and signed by authorized representatives of both parties.

All notices must be delivered in writing to the designated representative of each party. Electronic notices are effective upon acknowledgment of receipt.

AGREED AND ACCEPTED

Alder Technologies, Inc.                         Northstar Advisory, LLC
Authorized representative                       Authorized representative
'''),
 'mutual-nda': ('Mutual Non-Disclosure Agreement', '''MUTUAL NON-DISCLOSURE AGREEMENT

01  PURPOSE

The parties wish to evaluate a potential business relationship and may exchange confidential business, financial, and technical information for that purpose.

02  PERMITTED USE

The receiving party will use confidential information solely to evaluate the proposed relationship and will restrict disclosure to representatives who need to know and are subject to equivalent confidentiality obligations.

03  RETURN OF MATERIALS

Upon written request, the receiving party will return or destroy confidential materials within thirty (30) days, except copies required by law or maintained in routine backup systems.

04  TERM

This Agreement remains effective for two (2) years. The duty to protect disclosed information survives for three (3) years following termination.
'''),
 'consulting-addendum': ('Consulting Addendum · Statement of Work', '''STATEMENT OF WORK — CONSULTING ADDENDUM

01  DELIVERABLES

Provider will assess the existing document review workflow and deliver a written implementation plan, an interactive prototype, and a final handover session.

02  SCHEDULE

The engagement begins on October 12, 2026. The prototype will be delivered within thirty (30) days, subject to timely access to materials and stakeholder feedback.

03  ACCEPTANCE

Client will review deliverables within ten (10) business days and identify any material deficiencies in writing. Provider will address agreed deficiencies without additional charge.
''')}
with store.connection() as db:
    existing={row['id'] for row in db.execute('SELECT id FROM documents')}
for id,(title,text) in fixtures.items():
    if id not in existing:store.create_document(id,text,title)
    store.index_revision(id,store.get_document(id).version)
app=build_app(database_url=store.dsn)
# Submission returns as soon as PostgreSQL has durably accepted it.

original_lifespan=app.router.lifespan_context
@asynccontextmanager
async def lifespan(app):
    async def process():
        while True:
            await asyncio.to_thread(store.expire)
            await asyncio.to_thread(store.process_next)
            with store.connection() as db:
                heads=[(r['id'],r['version']) for r in db.execute('SELECT id,version FROM documents')]
            for id,version in heads:await asyncio.to_thread(store.index_revision,id,version)
            await asyncio.sleep(.1)
    task=asyncio.create_task(process())
    async with original_lifespan(app):
        try:yield
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
app.router.lifespan_context=lifespan
if __name__=='__main__':uvicorn.run(app,host='127.0.0.1',port=8010)
