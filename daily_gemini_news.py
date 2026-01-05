import os
import requests
import smtplib
import traceback
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import google.generativeai as genai
from datetime import datetime, timedelta

# --- CONFIGURATION ---
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

SOURCES = [
    "reuters.com", "aljazeera.com", "bbc.co.uk", "scmp.com", 
    "dw.com", "bloomberg.com", "wsj.com", "theguardian.com", 
    "france24.com", "thehindu.com", "apnews.com", "cnbc.com",
    "ft.com", "economist.com"
]

def get_working_model():
    genai.configure(api_key=GEMINI_API_KEY)
    preferred = ['gemini-1.5-pro-latest', 'gemini-1.5-pro', 'gemini-1.5-flash-latest', 'gemini-1.5-flash']
    try:
        available = [m.name.replace("models/", "") for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        for p in preferred:
            if p in available:
                return p
        return available[0] if available else None
    except:
        return None

def fetch_global_news():
    print("--- Fetching News (Last 7 Days) ---")
    domains = ",".join(SOURCES)
    date_from = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    
    url = (
        f"https://newsapi.org/v2/everything?"
        f"q=(economy OR politics OR inflation OR tariffs OR 'oil prices' OR geopolitics OR 'central bank' OR 'trade war') "
        f"AND (global OR international OR US OR China OR Europe OR 'Latin America')&"
        f"domains={domains}&from={date_from}&sortBy=publishedAt&language=en&pageSize=100&apiKey={NEWS_API_KEY}"
    )
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        articles = response.json().get("articles", [])
        print(f"Retrieved {len(articles)} articles.")
        return articles
    except Exception as e:
        print(f"Fetch error: {e}")
        return []

def analyze_news(articles):
    model_name = get_working_model()
    if not model_name:
        return None, "No model available."

    print(f"--- Analyzing with {model_name} ---")
    
    raw_text = ""
    for i, a in enumerate(articles):
        desc = a.get('description') or "No description"
        raw_text += (
            f"Article {i+1}:\nTitle: {a['title']}\nDescription: {desc}\n"
            f"Source: {a['source']['name']}\nDate: {a['publishedAt'][:10]}\nURL: {a['url']}\n\n"
        )

    model = genai.GenerativeModel(model_name)
    
    safety_settings = [{"category": c, "threshold": "BLOCK_NONE"} for c in [
        "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT"
    ]]

    prompt = (
        "You are a world-class strategic macro analyst mentoring an aspiring billionaire. "
        "Use ONLY the provided articles. Do not invent facts. Remain concise (max 10-min read).\n\n"

        "Structure the HTML report exactly as follows:\n\n"

        "<h3>Deep Dive: Billionaire Thinking & Implications</h3>\n"
        "<p>Teach 4–5 key principles from top billionaires' recommended books (e.g., Buffett: The Intelligent Investor; Thiel/Musk: Zero to One; Gates: Sapiens; Grove: High Output Management). "
        "Apply each directly to this week's news. For each:</p>\n"
        "<ul>\n"
        "  <li><strong>Principle & Book</strong>: Brief theory/logic.</li>\n"
        "  <li><strong>Application to This Week's News</strong>: How it connects.</li>\n"
        "  <li><strong>Implications</strong>: For personal wealth building + global impact.</li>\n"
        "</ul>\n\n"

        "<h3>Key Political Developments</h3>\n"
        "For the 5–8 most important events, create a box using <div class='section'>:\n"
        "<div class='section'>\n"
        "  <p class='news-title'>Short Event Summary</p>\n"
        "  <p><strong>Link:</strong> <a href='URL'>Source Name - Date</a></p>\n"
        "  <p><strong>Implications:</strong> For you (wealth opportunities/risks) • For the world</p>\n"
        "</div>\n\n"

        "End with:\n"
        "<h3>Transparent Sources (Top 30)</h3>\n"
        "<ul>list of links</ul>\n\n"

        "Tone: Professional, educational, actionable. Use only provided CSS classes.\n\n"

        f"RAW INTEL:\n{raw_text}"
    )

    try:
        generation_config = {
            "temperature": 0.2,
            "top_p": 0.95,
            "top_k": 64,
            "max_output_tokens": 8192,
        }

        response = model.generate_content(prompt, generation_config=generation_config, safety_settings=safety_settings)
        clean_html = response.text.strip().replace("```html", "").replace("```", "").strip()

        # Append sources if not already included robustly
        sources_html = "<h3>Transparent Sources (Top 30 Articles)</h3><ul>"
        for a in articles[:30]:
            sources_html += f'<li><a href="{a["url"]}">{a["title"]} — {a["source"]["name"]} ({a["publishedAt"][:10]})</a></li>'
        sources_html += "</ul>"
        clean_html += sources_html

        return clean_html, None
    except Exception:
        return None, traceback.format_exc()

def create_fallback_html(articles, error):
    html = f"<div style='background:#fee;padding:15px;border:1px solid red;'><h3>Analysis Failed</h3><pre>{error}</pre></div>"
    html += "<h3>Raw Articles (Top 20)</h3><ul>"
    for a in articles[:20]:
        html += f"<li><a href='{a['url']}'>{a['title']}</a> — {a['source']['name']} ({a['publishedAt'][:10]})</li>"
    html += "</ul>"
    return html

def send_email(html_content, prefix=""):
    print("--- Sending Email ---")
    try:
        with open('email_template.html', 'r', encoding='utf-8') as f:
            template = f.read()
    except FileNotFoundError:
        template = """<!DOCTYPE html>
<html><head><style>
    body { font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; color: #333; line-height: 1.6; max-width: 600px; margin: 0 auto; padding: 20px; }
    .header { background-color: #2c3e50; color: #ffffff; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
    .header h1 { margin: 0; font-size: 24px; }
    .date { font-size: 14px; opacity: 0.8; margin-top: 5px; }
    .section { background-color: #f9f9f9; padding: 15px; border-left: 5px solid #3498db; margin-bottom: 20px; border-radius: 4px; }
    .news-title { font-size: 18px; font-weight: bold; color: #2c3e50; margin-bottom: 5px; }
    .theory-box { background-color: #fff3cd; border: 1px solid #ffeeba; padding: 10px; font-size: 14px; margin-top: 10px; border-radius: 4px; color: #856404; }
    .footer { font-size: 12px; text-align: center; color: #888; margin-top: 30px; border-top: 1px solid #eee; padding-top: 20px; }
    a { color: #3498db; text-decoration: none; }
</style></head><body>
    <div class="header"><h1>🌍 Global Intelligence Brief</h1><div class="date">{{DATE}}</div></div>
    <div style="padding: 20px 0;">{{CONTENT}}</div>
    <div class="footer">Automated Analysis by Gemini • Multi-Source Global Monitor</div>
</body></html>"""

    body = template.replace("{{DATE}}", datetime.now().strftime('%B %d, %Y').upper())
    body = body.replace("{{CONTENT}}", html_content)

    msg = MIMEMultipart('alternative')
    msg['From'] = EMAIL_USER
    msg['To'] = RECIPIENT_EMAIL
    msg['Subject'] = f"{prefix} Global Intelligence Brief – {datetime.now().strftime('%Y-%m-%d')}"

    msg.attach(MIMEText(body, 'html'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print("Email sent.")
    except Exception as e:
        print(f"Email failed: {e}")

if __name__ == "__main__":
    articles = fetch_global_news()
    if not articles:
        send_email("<p>No news retrieved.</p>", "[NO DATA]")
    else:
        html, err = analyze_news(articles)
        if html:
            send_email(html)
        else:
            send_email(create_fallback_html(articles, err), "[ERROR]")