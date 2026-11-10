import os
import html
import json
import time
import smtplib
from typing import Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pydantic import BaseModel
from google import genai
from google.genai import types

# Modelos oficiales soportados en el tier gratuito
FALLBACK_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
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

# --- 2. CONSULTA A GEMINI EN DOS PASOS (ANTI-429) ---
def search_jobs(api_key: str) -> list[JobPosting]:
    client = genai.Client(api_key=api_key)

    # 1. Prompt de Búsqueda Web (conciso para el motor de búsqueda)
    search_prompt = (
        "Search for active software developer and IT job vacancies in the Dominican Republic "
        "(prioritize Santo Domingo and Remote/Hybrid open to DR candidates). "
        "Focus on Junior, Entry-Level, and Early-Career roles (0-3 years experience) in: "
        "Python, Backend, Full Stack, Web, Odoo/ERP, Automation, QA, and Data. "
        "Check LinkedIn, Computrabajo, Aldaba, Indeed, and local tech boards. "
        "For each position found, list: job title, company, location/modality, "
        "salary (note if confirmed under RD$30,000/month), requirements, tech stack, and direct application URL. "
        "Exclude expired or fabricated postings. If no active vacancies are found, respond exactly: NO_JOBS_FOUND."
    )

    print("🔎 Paso 1: Ejecutando búsqueda web con Google Search...")
    raw_search_text = ""
    last_error = None

    for model_name in FALLBACK_MODELS:
        try:
            print(f"🤖 Buscando en la web con {model_name}...")
            response = client.models.generate_content(
                model=model_name,
                contents=search_prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.2,
                ),
            )
            if response.text and response.text.strip():
                raw_search_text = response.text.strip()
                print(f"✅ Búsqueda web completada con {model_name}.")
                break
            raise ValueError(f"El modelo {model_name} devolvió una respuesta vacía.")
        except Exception as e:
            last_error = e
            print(f"⚠️ Falló búsqueda en {model_name}: {e}")
            print("⏳ Esperando 10 segundos...")
            time.sleep(10)

    if not raw_search_text:
        if last_error is not None:
            raise RuntimeError(
                f"❌ Todos los modelos de Gemini fallaron en el Paso 1. Último error: {last_error}"
            )
        print("ℹ️ No se obtuvo texto de búsqueda.")
        return []

    if raw_search_text == "NO_JOBS_FOUND" or (
        len(raw_search_text) < 50 and "NO_JOBS_FOUND" in raw_search_text
    ):
        print("ℹ️ No se detectaron vacantes nuevas en la web.")
        return []

    # Breve pausa para reiniciar la ventana por minuto
    time.sleep(5)

    # 2. Paso 2: Conversión a JSON Estricto (Sin búsqueda web = 0 riesgo de 429)
    print("📋 Paso 2: Estructurando resultados en formato JSON con Pydantic...")
    parse_prompt = (
        "Extract the job vacancies from the text below into the requested JSON schema.\n"
        "Rules:\n"
        "- Set salary_below_30k to true ONLY if confirmed monthly salary is under RD$30,000; "
        "false if >= RD$30,000; null if undisclosed.\n"
        "- Only include positions that have a real company and a verifiable application link.\n\n"
        f"Jobs raw data:\n{raw_search_text}"
    )

    parse_error = None
    for model_name in FALLBACK_MODELS:
        try:
            print(f"🤖 Estructurando JSON con {model_name}...")
            parse_response = client.models.generate_content(
                model=model_name,
                contents=parse_prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=JobList,
                    temperature=0.1,
                ),
            )
            if not parse_response.text:
                raise ValueError(f"El modelo {model_name} devolvió un JSON vacío.")
            parsed = JobList.model_validate_json(parse_response.text)
            print(f"✅ JSON estructurado con éxito usando {model_name}.")
            return parsed.vacancies
        except Exception as e:
            parse_error = e
            print(f"⚠️ Error al estructurar a JSON con {model_name}: {e}")
            print("⏳ Esperando 5 segundos...")
            time.sleep(5)

    raise RuntimeError(
        f"❌ Todos los modelos fallaron al estructurar el JSON en el Paso 2. Último error: {parse_error}"
    )

# --- 3. GESTION DE ESTADO (DEDUPLICACION) ---
def load_seen_urls(history_file="seen_jobs.json") -> set[str]:
    if os.path.exists(history_file):
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except json.JSONDecodeError:
            return set()
    return set()

def filter_new_vacancies(vacancies: list[JobPosting], history_file="seen_jobs.json") -> list[JobPosting]:
    seen_urls = load_seen_urls(history_file)
    new_items = []
    batch_urls = set()

    for job in vacancies:
        url = job.url.strip() if job.url else ""
        if url and url not in seen_urls and url not in batch_urls:
            batch_urls.add(url)
            new_items.append(job)

    return new_items

def save_seen_vacancies(new_jobs: list[JobPosting], history_file="seen_jobs.json"):
    seen_urls = load_seen_urls(history_file)
    seen_urls.update(job.url.strip() for job in new_jobs if job.url)
    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(sorted(list(seen_urls)), f, indent=2, ensure_ascii=False)

# --- 4. ENVIO DE CORREO SMTP ---
def send_email_alert(new_jobs: list[JobPosting]):
    sender = os.environ["EMAIL_SENDER"]
    receiver = os.environ["EMAIL_RECEIVER"]
    app_password = os.environ["EMAIL_APP_PASSWORD"]

    rows = []
    for job in new_jobs:
        alert_tag = (
            "<span style='color: #d93025; font-weight: bold;'> [⚠️ &lt; RD$30k]</span>"
            if job.salary_below_30k
            else ""
        )
        safe_url = html.escape(job.url.strip(), quote=True)
        safe_title = html.escape(job.title)
        safe_company = html.escape(job.company)
        safe_location = html.escape(job.location)
        safe_salary = html.escape(job.salary)
        safe_reqs = html.escape(job.experience_requirements)
        safe_stack = html.escape(job.key_technologies)

        rows.append(f"""
        <div style="padding: 14px; border-bottom: 1px solid #e0e0e0; font-family: sans-serif;">
            <h3 style="margin: 0 0 6px 0;">
                <a href="{safe_url}" style="color: #1a73e8; text-decoration: none;">{safe_title}</a>
            </h3>
            <p style="margin: 2px 0; color: #333;"><strong>Empresa:</strong> {safe_company} | <strong>Ubicación:</strong> {safe_location}</p>
            <p style="margin: 2px 0; color: #333;"><strong>Salario:</strong> {safe_salary}{alert_tag}</p>
            <p style="margin: 2px 0; color: #555;"><strong>Requisitos:</strong> {safe_reqs}</p>
            <p style="margin: 2px 0; color: #555;"><strong>Stack:</strong> <code>{safe_stack}</code></p>
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

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, app_password)
        server.sendmail(sender, receiver, msg.as_string())

# --- 5. PUNTO DE ENTRADA ---
if __name__ == "__main__":
    required_vars = ["GEMINI_API_KEY", "EMAIL_SENDER", "EMAIL_RECEIVER", "EMAIL_APP_PASSWORD"]
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    if missing_vars:
        raise ValueError(f"Faltan configurar las variables de entorno: {', '.join(missing_vars)}")

    api_key = os.environ["GEMINI_API_KEY"]

    print("🔎 Buscando vacantes activas...")
    all_jobs = search_jobs(api_key)
    print(f"Total devuelto por Gemini: {len(all_jobs)}")

    new_jobs = filter_new_vacancies(all_jobs)

    if new_jobs:
        print(f"🚀 {len(new_jobs)} vacantes no repetidas. Enviando notificación...")
        send_email_alert(new_jobs)
        save_seen_vacancies(new_jobs)
        print("✅ Correo enviado e historial actualizado con éxito.")
    else:
        print("🤫 No hay vacantes nuevas o ya fueron notificadas. Finalizando en silencio.")
