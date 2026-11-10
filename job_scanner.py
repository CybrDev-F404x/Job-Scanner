import os
import json
import time
import smtplib
from typing import Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pydantic import BaseModel
from google import genai

# Lee la clave inyectada por el entorno
api_key = os.getenv("GEMINI_API_KEY")

# Lista de modelos disponibles en el tier gratuito en orden de prioridad (fallback automatico)
FALLBACK_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.1-pro-preview",
]

# --- 1. MODELOS DE DATOS ---
class JobPosting(BaseModel):
    title: str
    company: str
    location: str
    salary: str
    salary_below_30k: Optional[bool] = None
    experience_requirements: str
    key_technologies: str
    url: str

class JobList(BaseModel):
    vacancies: list[JobPosting]

# --- 2. CONSULTA A GEMINI CON FALLBACK DE MODELOS ---
def search_jobs(api_key: str) -> list[JobPosting]:
    client = genai.Client(api_key=api_key)

    prompt = (
    "Target: Active software and technology job vacancies in Dominican Republic "
    "(priority: Santo Domingo, Remote/Hybrid open to DR residents).\n"
    "Candidate profile: Junior/Entry-level developer with skills in Python, FastAPI, "
    "PostgreSQL, Docker, Power Platform, and AI-assisted tooling. "
    "Accept roles requiring 0 to 3 years experience (evaluate fit via projects/skills).\n\n"
    "Target Roles by Priority:\n"
    "- P1 (Software Dev): Junior Software/Backend/Python/Full Stack/Web/API Developer, Odoo/ERP/Integration Dev.\n"
    "- P2 (Automation/Data/AI): Automation Dev, RPA, Power Platform Specialist, QA Automation, Junior Data/AI Eng.\n"
    "- P3 (Adjacent IT): Implementation Specialist, Application Support, Junior Systems Analyst.\n\n"
    "Search Sources & Verification:\n"
    "- Search: LinkedIn, Computrabajo, Aldaba, Indeed, Get on Board, and official company portals.\n"
    "- Verify: Must be active, verifiable postings with valid application URLs. Consolidate duplicates. Never fabricate listings.\n\n"
    "Evaluation & Salary Rules:\n"
    "- Minimum target: RD$30,000/month. Set salary_below_30k=true ONLY if confirmed < RD$30,000; "
    "false if >= RD$30,000; null if undisclosed/unverified. Do not exclude unlisted salaries.\n"
    "- Recommendation values: 'Apply', 'Consider applying', or 'Low priority'.\n"
    "- Rank output by software relevance, candidate fit, and career growth potential.\n"
    "- Return an empty list if no verified active positions match."
)

    last_error = None

    for model_name in FALLBACK_MODELS:
        try:
            print(f"🤖 Intentando consulta con el modelo: {model_name}...")
            interaction = client.interactions.create(
                model=model_name,
                input=prompt,
                tools=[{"type": "google_search"}],
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": JobList.model_json_schema(),
                },
            )

            if not interaction.output_text:
                raise ValueError(f"El modelo {model_name} devolvió una respuesta vacía.")

            parsed = JobList.model_validate_json(interaction.output_text)
            print(f"✅ Respuesta obtenida exitosamente con {model_name}.")
            return parsed.vacancies

        except Exception as e:
            last_error = e
            print(f"⚠️ Falló el modelo {model_name} (cuota agotada o sin respuesta): {e}")
            print("🔄 Cambiando al siguiente modelo disponible...")
            time.sleep(2)

    raise RuntimeError(
        f"❌ Todos los modelos de Gemini fallaron. Último error: {last_error}"
    )

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