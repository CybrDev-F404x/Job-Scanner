import os
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pydantic import BaseModel
from google import genai
from google.genai import types

# Lee la clave inyectada por el entorno
api_key = os.getenv("GEMINI_API_KEY")

# --- 1. MODELOS DE DATOS ---
class JobPosting(BaseModel):
    title: str
    company: str
    location: str
    salary: str
    salary_below_30k: bool
    experience_requirements: str
    key_technologies: str
    url: str

class JobList(BaseModel):
    vacancies: list[JobPosting]

# --- 2. CONSULTA A GEMINI ---
def search_jobs(api_key: str) -> list[JobPosting]:
    client = genai.Client(api_key=api_key)

    
    prompt = (
        "Search for current, active job vacancies in the Dominican Republic, "
        "prioritizing Santo Domingo and remote or hybrid roles open to candidates "
        "residing in the Dominican Republic. "

        "The candidate is an ITLA Software Development student with completed "
        "coursework, practical exposure to Python, FastAPI, PostgreSQL, Docker, "
        "Power Platform, SharePoint, Power Apps, Power Automate, and software "
        "development projects. The candidate can leverage AI tools for coding, "
        "debugging, automation, learning, and productivity. Consider AI-assisted "
        "development a competitive advantage, without assuming it replaces "
        "technical fundamentals or professional experience. "

        "PRIORITY 1 - SOFTWARE DEVELOPMENT: "
        "Junior Software Developer, Software Engineer, Backend Developer, "
        "Python Developer, Full Stack Developer, Web Developer, API Developer, "
        "Application Developer, Odoo Developer, ERP Developer, Integration Developer. "

        "PRIORITY 2 - AUTOMATION, DATA, AND AI: "
        "Automation Developer, Python Scripting Developer, RPA Developer, "
        "Power Platform Developer, Workflow Automation Specialist, "
        "AI Automation Developer, Junior AI Developer, Junior Data Engineer, "
        "Technical Data Analyst, QA Automation Engineer. "

        "PRIORITY 3 - ADJACENT TECHNOLOGY ROLES: "
        "Application Support Analyst, Software Implementation Specialist, "
        "Technical Application Specialist, ERP/CRM Implementation Consultant, "
        "Junior Systems Analyst, Technical Support Engineer, IT Specialist, "
        "and entry-level infrastructure or networking positions when they "
        "offer relevant technical experience or career growth. "

        "Target entry-level, junior, trainee, associate, internship, and "
        "early-career positions. Include vacancies requesting 1-3 years "
        "of experience if the candidate could reasonably qualify through "
        "projects, internships, coursework, or practical skills. "
        "Do not automatically exclude positions requiring experience. "

        "The preferred minimum salary is RD$30,000 monthly. "
        "Set salary_below_30k to true only when the confirmed monthly salary "
        "is below RD$30,000; false when it is RD$30,000 or higher; "
        "and null when salary is unknown or unverified. "
        "Never invent salary information or exclude vacancies solely because "
        "salary is not disclosed. "

        "Search reputable sources including LinkedIn Jobs, Computrabajo, "
        "Aldaba, Indeed, Get on Board, Wellfound, official company career pages, "
        "and other reliable Dominican or international job boards. "

        "Verify that each vacancy is active, has a real employer, and has "
        "a valid application URL. Prefer official employer application links. "
        "Do not fabricate vacancies, links, salaries, or requirements. "
        "Exclude expired, duplicate, or unverifiable listings. "

        "For each vacancy, return: job_title, company, location, "
        "work_modality, job_category, seniority, experience_required, "
        "salary, salary_below_30k, publication_date, source, application_url, "
        "active_status, candidate_fit, fit_reason, main_requirements, "
        "missing_skills, and recommendation. "

        "Set recommendation to Apply, Consider applying, or Low priority. "
        "Rank results by software development relevance, realistic candidate fit, "
        "salary, active status, and career growth potential. "

        "Avoid duplicates across sources. If the same vacancy appears "
        "on multiple platforms, consolidate it into one entry. "

        "Return only verified active vacancies in the expected structured "
        "output format. If no active vacancies are confirmed, return an empty list."
    )


    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            response_mime_type="application/json",
            response_schema=JobList,
            temperature=0.1,
        ),
    )

    # Validar y cargar con Pydantic
    parsed = JobList.model_validate_json(response.text)
    return parsed.vacancies

# --- 3. GESTION DE ESTADO (DEDUPLICACION) ---
def filter_new_vacancies(vacancies: list[JobPosting], history_file="seen_jobs.json") -> list[JobPosting]:
    # Si existe el archivo, cargamos las URLs ya vistas
    if os.path.exists(history_file):
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                seen_urls = set(json.load(f))
        except json.JSONDecodeError:
            seen_urls = set()
    else:
        seen_urls = set()

    new_items = [job for job in vacancies if job.url and job.url not in seen_urls]

    # Actualizamos el historial sumando las nuevas URLs
    if new_items:
        seen_urls.update(job.url for job in new_items)
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(sorted(list(seen_urls)), f, indent=2, ensure_ascii=False)

    return new_items

# --- 4. ENVIO DE CORREO SMTP ---
def send_email_alert(new_jobs: list[JobPosting]):
    sender = os.environ["EMAIL_SENDER"]
    receiver = os.environ["EMAIL_RECEIVER"]
    app_password = os.environ["EMAIL_APP_PASSWORD"]

    # Construccion de plantilla HTML limpia
    rows = []
    for job in new_jobs:
        alert_tag = (
            "<span style='color: #d93025; font-weight: bold;'> [⚠️ < RD$30k]</span>"
            if job.salary_below_30k
            else ""
        )
        rows.append(f"""
        <div style="padding: 14px; border-bottom: 1px solid #e0e0e0; font-family: sans-serif;">
            <h3 style="margin: 0 0 6px 0;">
                <a href="{job.url}" style="color: #1a73e8; text-decoration: none;">{job.title}</a>
            </h3>
            <p style="margin: 2px 0; color: #333;"><strong>Empresa:</strong> {job.company} | <strong>Ubicación:</strong> {job.location}</p>
            <p style="margin: 2px 0; color: #333;"><strong>Salario:</strong> {job.salary}{alert_tag}</p>
            <p style="margin: 2px 0; color: #555;"><strong>Requisitos:</strong> {job.experience_requirements}</p>
            <p style="margin: 2px 0; color: #555;"><strong>Stack:</strong> <code>{job.key_technologies}</code></p>
        </div>
        """)

    html_body = f"""
    <html>
      <body style="font-family: Arial, sans-serif; max-width: 650px; margin: auto; padding: 20px;">
        <h2 style="color: #202124;">🎯 {len(new_jobs)} Nueva(s) Vacante(s) Encontrada(s)</h2>
        {''.join(rows)}
      </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📢 {len(new_jobs)} nueva(s) vacante(s) de desarrollo en RD"
    msg["From"] = sender
    msg["To"] = receiver
    msg.attach(MIMEText(html_body, "html"))

    # Conexion SSL estandar a Gmail
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, app_password)
        server.sendmail(sender, receiver, msg.as_string())

# --- 5. PUNTO DE ENTRADA ---
if __name__ == "__main__":
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Falta configurar la variable de entorno GEMINI_API_KEY")

    print("🔎 Buscando vacantes activas...")
    all_jobs = search_jobs(api_key)
    print(f"Total devuelto por Gemini: {len(all_jobs)}")

    new_jobs = filter_new_vacancies(all_jobs)

    if new_jobs:
        print(f"🚀 {len(new_jobs)} vacantes no repetidas. Enviando notificación...")
        send_email_alert(new_jobs)
        print("✅ Correo enviado con éxito.")
    else:
        print("🤫 No hay vacantes nuevas o ya fueron notificadas. Finalizando en silencio.")