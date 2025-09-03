#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import re
import ssl
import smtplib
import time
import logging
import json
import tempfile
import datetime as dt
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Optional, List, Tuple

import requests
from bs4 import BeautifulSoup

# PDF: preferimos pdfplumber; si falla, hacemos fallback a pdfminer
import pdfplumber
try:
    from pdfminer.high_level import extract_text as pm_extract
except Exception:
    pm_extract = None

# Sumario extractivo (no requiere NLTK si usamos el tokenizer de sumy)
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.lex_rank import LexRankSummarizer

# Traducción mejorada
try:
    from deep_translator import GoogleTranslator
except Exception:
    GoogleTranslator = None

# ---------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------

@dataclass
class Config:
    # Página de listados (Plan B)
    base_url: str = "https://www.ecdc.europa.eu/en/publications-and-data/monitoring/weekly-threats-reports"

    # Plantilla de URL directa por semana/año (Plan A)
    direct_pdf_template: str = (
        "https://www.ecdc.europa.eu/sites/default/files/documents/"
        "communicable-disease-threats-report-week-{week}-{year}.pdf"
    )

    # Patrón de PDF válido (para Plan B)
    pdf_regex: re.Pattern = re.compile(
        r"/communicable-disease-threats-report-week-(\d+)-(\d{4})\.pdf$"
    )

    # Nº de oraciones del sumario
    summary_sentences: int = 12

    # SMTP/Email (rellenado vía GitHub Secrets en el workflow)
    smtp_server: str = os.getenv("SMTP_SERVER", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "465") or "465")
    sender_email: str = os.getenv("SENDER_EMAIL", "")
    receiver_email: str = os.getenv("RECEIVER_EMAIL", "")
    email_password: str = os.getenv("EMAIL_PASSWORD", "")

    # Bandera para no enviar correo (tests): DRY_RUN=1
    dry_run: bool = os.getenv("DRY_RUN", "0") == "1"

    # Log level
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # Tamaño máximo opcional (MB) para abortar PDFs inusualmente grandes
    max_pdf_mb: int = 25

# ---------------------------------------------------------------------
# Agente
# ---------------------------------------------------------------------

class WeeklyReportAgent:
    """
    Pipeline:
      1) Intenta localizar el PDF de la semana actual (Plan A: URL directa).
      2) Si falla, rastrea la página de listados y localiza el PDF más reciente (Plan B).
      3) Descarga, extrae texto, resume (LexRank), traduce al español y envía email.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

        logging.basicConfig(
            level=getattr(logging, self.config.log_level.upper(), logging.INFO),
            format="%(levelname)s %(message)s"
        )

        # Sesión HTTP con reintentos
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/html,application/pdf,application/xhtml+xml,*/*;q=0.8",
        })
        adapter = requests.adapters.HTTPAdapter(
            max_retries=requests.packages.urllib3.util.retry.Retry(
                total=4,
                backoff_factor=0.6,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["HEAD", "GET"]),
            )
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # ------------------------ Localización del PDF ---------------------

    def _try_direct_weekly_pdf(self) -> Optional[str]:
        """Plan A: URL directa del PDF por semana ISO; retrocede hasta 4 semanas si hace falta."""
        today = dt.date.today()
        year, current_week, _ = today.isocalendar()

        # Probar desde la semana actual hasta 4 semanas atrás
        for weeks_back in range(0, 5):
            week_to_try = current_week - weeks_back
            year_to_try = year
            
            if week_to_try <= 0:
                # Ajustar para semanas del año anterior
                year_to_try = year - 1
                last_week_prev_year = dt.date(year_to_try, 12, 28).isocalendar()[1]
                week_to_try = last_week_prev_year + week_to_try  # week_to_try es negativo

            url = self.config.direct_pdf_template.format(week=week_to_try, year=year_to_try)
            try:
                h = self.session.head(url, timeout=10, allow_redirects=True)
                logging.debug("Probando semana %s-%s: %s -> %s", week_to_try, year_to_try, url, h.status_code)
                
                ct = h.headers.get("Content-Type", "").lower()
                content_length = h.headers.get("Content-Length", "0")
                
                # Verificar que sea PDF y tenga tamaño razonable (> 10KB)
                if (h.status_code == 200 and 
                    "pdf" in ct and 
                    int(content_length) > 10000):
                    logging.info("✅ PDF directo encontrado: semana %s-%s", week_to_try, year_to_try)
                    return url
                    
            except requests.RequestException as e:
                logging.debug("Error probando %s: %s", url, e)
                continue
                
        return None

        def _get_pdf_week_info(self, pdf_url: str) -> Optional[Tuple[int, int]]:
        """Extrae información de semana y año del URL del PDF"""
        m = self.config.pdf_regex.search(pdf_url)
        if m:
            week = int(m.group(1))
            year = int(m.group(2))
            return (year, week)
        return None
        
    def _scan_listing_page(self) -> Optional[str]:
        """Plan B: rastrea la página de listados y devuelve el PDF más reciente."""
        try:
            r = self.session.get(self.config.base_url, timeout=20)
            r.raise_for_status()
        except requests.RequestException as e:
            logging.warning("No se pudo cargar la página de listados: %s", e)
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        candidates: List[Tuple[dt.datetime, str]] = []  # Cambiamos para usar datetime

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href:
                continue
            if not href.startswith("http"):
                href = requests.compat.urljoin(self.config.base_url, href)

            m = self.config.pdf_regex.search(href)
            if m:
                week = int(m.group(1))
                year = int(m.group(2))
                
                # Convertir año/semana a datetime para ordenar correctamente
                try:
                    # Primer día de la semana ISO
                    pdf_date = dt.datetime.fromisocalendar(year, week, 1)
                    
                    try:
                        h = self.session.head(href, timeout=12, allow_redirects=True)
                        ct = h.headers.get("Content-Type", "").lower()
                        if h.status_code == 200 and "pdf" in ct:
                            candidates.append((pdf_date, href))
                            logging.debug("Candidato OK: %s (semana %s-%s)", href, week, year)
                    except requests.RequestException:
                        continue
                        
                except ValueError:
                    continue

        if not candidates:
            return None

        # Ordenar por fecha descendente (más reciente primero)
        candidates.sort(key=lambda x: x[0], reverse=True)
        
        # Tomar el más reciente
        best_date, best_url = candidates[0]
        logging.info("PDF más reciente encontrado: %s (semana %s)", best_url, best_date.isocalendar()[1])
        
        return best_url

        candidates.sort(reverse=True)  # por (year, week) desc
        _, _, best = candidates[0]
        return best

    def fetch_latest_pdf_url(self) -> Optional[str]:
        """Intenta Plan A; si falla, Plan B."""
        url = self._try_direct_weekly_pdf()
        if url:
            logging.info("PDF directo encontrado: %s", url)
            return url

        url = self._scan_listing_page()
        if url:
            logging.info("PDF por listado encontrado: %s", url)
        else:
            logging.info("No se encontró PDF nuevo.")
        return url

    # --------------------- Descarga / extracción -----------------------

    def download_pdf(self, pdf_url: str, dest_path: str, max_mb: int = 25) -> None:
        """Descarga el PDF (si servidor devuelve HTML, reintenta con ?download=1)."""

        def _append_download_param(url: str) -> str:
            return url + ("&download=1" if "?" in url else "?download=1")

        def _looks_like_pdf(first_bytes: bytes) -> bool:
            return first_bytes.startswith(b"%PDF")

        # 1) HEAD opcional para tamaño
        try:
            h = self.session.head(pdf_url, timeout=15, allow_redirects=True)
            clen = h.headers.get("Content-Length")
            if clen and int(clen) > max_mb * 1024 * 1024:
                raise RuntimeError(
                    f"El PDF excede {max_mb} MB ({int(clen)/1024/1024:.1f} MB)"
                )
        except requests.RequestException:
            pass

        headers = {
            "Accept": "application/pdf",
            "Referer": self.config.base_url,
            "Cache-Control": "no-cache",
        }

        def _try_get(url: str) -> Tuple[str, Optional[str], bytes]:
            r = self.session.get(url, headers=headers, stream=True, timeout=45, allow_redirects=True)
            r.raise_for_status()
            ct = r.headers.get("Content-Type", "")
            chunk_iter = r.iter_content(chunk_size=8192)
            first = next(chunk_iter, b"")
            with open(dest_path, "wb") as f:
                if first:
                    f.write(first)
                for chunk in chunk_iter:
                    if not chunk:
                        continue
                    f.write(chunk)
            return ct, r.headers.get("Content-Length"), first

        # 2) Primer intento
        try:
            ct, clen, first = _try_get(pdf_url)
            logging.debug("GET %s -> Content-Type=%s, len=%s", pdf_url, ct, clen)
            if ("pdf" in (ct or "").lower()) and _looks_like_pdf(first):
                return
            logging.info("Respuesta no-PDF. Reintentando con ?download=1 ...")
        except requests.RequestException as e:
            logging.info("Fallo en GET inicial (%s). Reintentamos con ?download=1 ...", e)

        # 3) Segundo intento con ?download=1
        retry_url = _append_download_param(pdf_url)
        ct2, clen2, first2 = _try_get(retry_url)
        logging.debug("GET %s -> Content-Type=%s, len=%s", retry_url, ct2, clen2)
        if ("pdf" in (ct2 or "").lower()) and _looks_like_pdf(first2):
            return

        # 4) Error final
        raise RuntimeError(
            f"No se obtuvo un PDF válido (Content-Type={ct2!r}, firma={first2[:8]!r})."
        )

    def extract_text(self, pdf_path: str) -> str:
        """Extrae texto con pdfplumber y, si falla, con pdfminer (si está disponible)."""
        try:
            with pdfplumber.open(pdf_path) as pdf:
                full_text = ""
                for page in pdf.pages:
                    full_text += page.extract_text() or ""
                return full_text
        except Exception as e:
            logging.warning("Fallo pdfplumber: %s. Usando pdfminer...", e)
            if pm_extract:
                try:
                    return pm_extract(pdf_path)
                except Exception as pm_e:
                    logging.error("Fallo pdfminer: %s", pm_e)
                    return ""
            else:
                logging.error("pdfminer no instalado.")
                return ""

    # -------------------------- Sumario --------------------------------

    def summarize(self, text: str, sentences: int) -> str:
        if not text.strip():
            return ""
        sentences = max(1, sentences)
        parser = PlaintextParser.from_string(text, Tokenizer("english"))
        summarizer = LexRankSummarizer()
        sents = summarizer(parser.document, sentences)
        return " ".join(str(s) for s in sents)

    # ------------------------- Traducción -------------------------------

    def translate_to_spanish(self, text: str) -> str:
        if not text.strip():
            return text
        if GoogleTranslator is None:
            return text
        try:
            return GoogleTranslator(source='auto', target='es').translate(text)
        except Exception as e:
            logging.warning("Fallo traducción: %s", e)
            return text

    # ------------------------- Manejo de estado -------------------------

    def _get_last_processed_url(self) -> Optional[str]:
        state_file = ".agent_state.json"
        try:
            if os.path.exists(state_file):
                with open(state_file, 'r') as f:
                    data = json.load(f)
                    return data.get('last_url')
        except Exception:
            pass
        return None

    def _save_processed_url(self, url: str):
        state_file = ".agent_state.json"
        try:
            with open(state_file, 'w') as f:
                json.dump({'last_url': url, 'timestamp': time.time()}, f)
        except Exception:
            pass

    # ------------------------- Email -----------------------------------

    def build_html(self, summary_es: str, pdf_url: str) -> str:
        return f"""
        <html>
          <body style="font-family:Arial,Helvetica,sans-serif;line-height:1.5;background:#f7f7f7;padding:18px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;margin:auto;background:#ffffff;border-radius:8px;overflow:hidden;">
              <tr>
                <td style="background:#005ba4;color:#fff;padding:18px 20px;">
                  <h1 style="margin:0;font-size:22px;">Boletín semanal de amenazas sanitarias</h1>
                  <p style="margin:6px 0 0 0;font-size:14px;opacity:.9;">Resumen automático del informe ECDC</p>
                </td>
              </tr>
              <tr>
                <td style="padding:20px;font-size:15px;color:#222;">
                  <p style="margin-top:0;white-space:pre-wrap">{summary_es}</p>
                  <p style="margin-top:18px">
                    Enlace al informe:&nbsp;
                    <a href="{pdf_url}" style="color:#005ba4;text-decoration:underline">{pdf_url}</a>
                  </p>
                </td>
              </tr>
              <tr>
                <td style="background:#f0f0f0;color:#666;padding:12px 16px;text-align:center;font-size:12px;">
                  Generado automáticamente · {dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
                </td>
              </tr>
            </table>
          </body>
        </html>
        """.strip()

    def send_email(self, subject: str, plain: str, html: Optional[str] = None) -> None:
        if not self.config.sender_email or not self.config.receiver_email:
            raise ValueError("Faltan SENDER_EMAIL o RECEIVER_EMAIL.")
        if not self.config.smtp_server:
            raise ValueError("Falta SMTP_SERVER.")

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.config.sender_email
        msg["To"] = self.config.receiver_email
        msg.set_content(plain or "(vacío)")
        if html:
            msg.add_alternative(html, subtype="html")

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(self.config.smtp_server, self.config.smtp_port, context=context) as server:
            if self.config.email_password:
                server.login(self.config.sender_email, self.config.email_password)
            server.send_message(msg)

    # --------------------------- Debug mejorado -------------------------

    def _debug_logging(self, pdf_url: Optional[str], text: str, summary_en: str, summary_es: str) -> None:
        """Logging controlado para no saturar los logs"""
        logging.info("=" * 60)
        logging.info("📊 DEBUG - ESTADO DEL AGENTE")
        logging.info("=" * 60)
        
        # 1. Configuración
        logging.info("⚙️ CONFIGURACIÓN:")
        logging.info("   SMTP Server: %s", self.config.smtp_server)
        logging.info("   SMTP Port: %s", self.config.smtp_port)
        logging.info("   From: %s", self.config.sender_email)
        logging.info("   To: %s", self.config.receiver_email)
        logging.info("   Password configurada: %s", "SÍ" if self.config.email_password else "NO")
        
        # 2. PDF
        logging.info("📄 PDF:")
        logging.info("   URL encontrada: %s", pdf_url if pdf_url else "NO")
        
        # 3. Texto
        logging.info("📝 TEXTO:")
        logging.info("   Caracteres extraídos: %d", len(text))
        if len(text) > 100:
            logging.info("   Preview: %s...", text[:100].replace("\n", " "))
        
        # 4. Resumen
        logging.info("🔍 RESUMEN:")
        logging.info("   Caracteres resumen EN: %d", len(summary_en))
        if summary_en and len(summary_en) > 50:
            logging.info("   Preview EN: %s...", summary_en[:50])
        logging.info("   Caracteres resumen ES: %d", len(summary_es))
        if summary_es and len(summary_es) > 50:
            logging.info("   Preview ES: %s...", summary_es[:50])
        
        logging.info("=" * 60)



    def run(self) -> None:   
        import nltk
        nltk.data.path.append('/home/runner/nltk_data')
        nltk.data.path.append('$HOME/nltk_data')
        
        logging.info("🚀 Iniciando agente ECDC")
        
        # Verificar configuración esencial
        if not all([self.config.smtp_server, self.config.sender_email, self.config.receiver_email]):
            logging.error("❌ CONFIGURACIÓN FALTANTE: Revisa SMTP_SERVER, SENDER_EMAIL, RECEIVER_EMAIL")
            return
        
        # Lee SUMMARY_SENTENCES si está
        ss_env = os.getenv("SUMMARY_SENTENCES")
        if ss_env and ss_env.strip().isdigit():
            self.config.summary_sentences = int(ss_env.strip())

           def run(self) -> None:
        # ... código existente al principio ...
        
        pdf_url = self.fetch_latest_pdf_url()
        if not pdf_url:
            logging.info("No hay PDF nuevo o no se encontró ninguno.")
            return

        # Verificar información del PDF encontrado
        pdf_info = self._get_pdf_week_info(pdf_url)
        if pdf_info:
            year, week = pdf_info
            current_year, current_week, _ = dt.date.today().isocalendar()
            logging.info("📅 PDF encontrado: semana %s del año %s", week, year)
            logging.info("📅 Semana actual: semana %s del año %s", current_week, current_year)
            
            # Verificar si es de la semana actual o anterior
            if year == current_year and week == current_week:
                logging.info("✅ PDF de la semana actual")
            elif year == current_year and week == current_week - 1:
                logging.info("ℹ️ PDF de la semana pasada")
            else:
                logging.warning("⚠️ PDF puede estar desactualizado")

        # Verificar si ya procesamos este URL
        last_url = self._get_last_processed_url()
        if last_url == pdf_url:
            logging.info("PDF ya procesado anteriormente: %s", pdf_url)
            return

        tmp_path = ""
        text = ""
        summary_en = ""
        summary_es = ""
        
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp_path = tmp.name

            # Descargar
            try:
                logging.info("⬇️ Descargando PDF...")
                self.download_pdf(pdf_url, tmp_path, max_mb=self.config.max_pdf_mb)
                logging.info("✅ PDF descargado")
            except Exception as e:
                logging.error("❌ Fallo descargando el PDF: %s", e)
                return

            # Extraer
            try:
                logging.info("📖 Extrayendo texto...")
                text = self.extract_text(tmp_path) or ""
                logging.info("✅ Texto extraído: %d caracteres", len(text))
            except Exception as e:
                logging.error("❌ Fallo extrayendo texto: %s", e)
                text = ""
        finally:
            if tmp_path:
                for _ in range(3):
                    try:
                        os.remove(tmp_path)
                        break
                    except Exception:
                        time.sleep(0.2)

        if not text.strip():
            logging.warning("⚠️ El PDF no contiene texto extraíble.")
            # Debug incluso si falla
            self._debug_logging(pdf_url, text, "", "")
            return

        # Resumen
        try:
            logging.info("🧠 Generando resumen...")
            summary_en = self.summarize(text, self.config.summary_sentences)
            logging.info("✅ Resumen generado: %d caracteres", len(summary_en))
        except Exception as e:
            logging.error("❌ Fallo generando el resumen: %s", e)
            return

        if not summary_en.strip():
            logging.warning("⚠️ No se pudo generar resumen.")
            self._debug_logging(pdf_url, text, "", "")
            return

        # Traducción
        try:
            logging.info("🌍 Traduciendo...")
            summary_es = self.translate_to_spanish(summary_en)
            logging.info("✅ Traducción completada: %d caracteres", len(summary_es))
        except Exception as e:
            logging.warning("⚠️ Fallo traduciendo, uso original: %s", e)
            summary_es = summary_en

        # Mostrar debug controlado
        self._debug_logging(pdf_url, text, summary_en, summary_es)

        html = self.build_html(summary_es, pdf_url)
        subject = "Resumen del informe semanal del ECDC"

        if self.config.dry_run:
            logging.info("🔶 DRY_RUN=1: No se envía email. Asunto: %s", subject)
            return

        # Envío
        try:
            logging.info("📧 Enviando email...")
            self.send_email(subject, summary_es, html)
            logging.info("✅ Correo enviado correctamente.")
            self._save_processed_url(pdf_url)
        except Exception as e:
            logging.error("❌ Fallo enviando el email: %s", e)
            # Info adicional para errores de SMTP
            if "authentication" in str(e).lower():
                logging.error("💡 POSIBLE SOLUCIÓN: Revisa la contraseña de aplicación de Gmail")
            elif "connection" in str(e).lower():
                logging.error("💡 POSIBLE SOLUCIÓN: Revisa SMTP_SERVER y SMTP_PORT")

# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------

def main() -> None:
    cfg = Config()
    agent = WeeklyReportAgent(cfg)
    agent.run()

if __name__ == "__main__":
    main()
