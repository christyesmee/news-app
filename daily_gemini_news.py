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

# Broader source list for diverse perspectives (added apnews.com, etc.)
SOURCES = [
    "reuters.com", "aljazeera.com", "bbc.co.uk", "scmp.com", 
    "dw.com", "bloomberg.com", "wsj.com", "theguardian.com", 
    "france24.com", "thehindu.com", "apnews.com", "cnbc.com",
    "ft.com", "economist.com"
]

def get_working_model():
    """Selects the most capable Gemini model available."""
    print("--- Selecting Best Available Gemini Model ---")
    genai.configure(api_key=GEMINI_API_KEY)
    preferred_models = ['gemini-1.5-pro-latest', 'gemini-1.5-pro', 'gemini-1.5-flash-latest', 'gemini-1.5-flash']
    
    try:
        models = genai.list_models()
        available = [m.name.replace("models/", "") for m in models if 'generateContent' in m.supported_generation_methods]
        for pref in preferred_models:
            if pref in available:
                print(f"-> Using {pref}")
                return pref
        return available[0] if available else None
    except Exception as e:
        print(f"!! Model selection error: {e}")
        return None

def fetch_global_news():
    print("--- Fetching Recent International Economic & Political News ---")
    domains = ",".join(SOURCES)
    # 7-day window for wider weekly coverage
    date_from = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    
    # Refined query for macro relevance: added keywords like inflation, tariffs, oil prices, economic cycle
    url = (
        f"https://newsapi.org/v2/everything?"
        f"q=(economy OR politics OR inflation OR tariffs OR 'oil prices' OR 'economic cycle' OR recession OR "
        f"'central bank' OR 'interest rate' OR geopolitics OR 'trade war' OR 'supply chain' OR 'fiscal policy') "
        f"AND (global OR international OR world OR US OR China OR Europe OR emerging OR 'Latin America' OR 'Middle East')&"
        f"domains={domains}&"
        f"from={date_from}&"
        f"sortBy=publishedAt&"
        f"language=en&"
        f"pageSize=100&"  # Increased to 100 for wider coverage
        f"apiKey={NEWS_API_KEY}"
    )
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        articles = data.get("articles", [])
        print(f"-> Retrieved {len(articles)} articles from diverse sources.")
        return articles
    except Exception as e:
        print(f"!! News fetch error: {e}")
        return []

def analyze_news(articles):
    model_name = get_working_model()
    if not model_name:
        return None, "No Gemini models available."

    print(f"--- Analyzing with {model_name} ---")
    
    # Richer input: title + description + URL + source + date
    raw_text = ""
    for i, a in enumerate(articles):
        desc = a.get('description') or "No description available"
        raw_text += (
            f"Article {i+1}:\n"
            f"Title: {a['title']}\n"
            f"Description: {desc}\n"
            f"Source: {a['source']['name']}\n"
            f"Date: {a['publishedAt'][:10]}\n"
            f"URL: {a['url']}\n\n"
        )

    model = genai.GenerativeModel(model_name)
    
    # Safety settings for unfiltered analysis
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    # Optimized prompt: strict anti-hallucination, balance, factuality, diversity; aligned structure; no image placeholders
    prompt = (
        "You are an elite strategic macro analyst serving a sophisticated global investor. "
        "Synthesize STRICTLY from the provided article titles, descriptions, sources, dates, and URLs only. "
        "Do not add, invent, or assume any facts, events, numbers, or details not explicitly in the raw intel. "
        "If information is missing or limited, mark interpretations as 'limited based on available articles' or 'not covered in sources'. "
        "Remain balanced and objective—highlight differing perspectives from diverse sources (e.g., Western vs. Asian vs. Middle Eastern views) when present. "
        "Use established economic principles only to interpret facts directly from the intel; do not hallucinate. "
        "No introductions, conclusions, or chit-chat—start directly with the report structure.\n\n"

        "Output a concise Strategic World Report in clean HTML format (use <h3>, <h4>, <p>, <ul>, <li>; no other tags or styles).\n\n"

        "### REQUIRED STRUCTURE:\n"
        "1. **Key Political Developments** (<h3>)\n"
        "   - Bullet list (<ul><li>) of significant geopolitical and policy events. Focus on power shifts, conflicts, sanctions, and international reactions. Reference source diversity.\n\n"

        "2. **Economic Outlook** (<h3>)\n"
        "   - Summarize forecasts, market signals, commodity trends (e.g., oil), trade issues, and central bank actions from articles.\n\n"

        "3. **Interpretation & Strategic Insights** (<h3>)\n"
        "   - **Economic Cycles Interpretation** (<h4>): Assess global/regional cycle position (early/mid/late expansion, peak, contraction, recovery) grounded in article indicators (e.g., GDP, unemployment, inflation). Mark as limited if data sparse.\n"
        "   - **Global Inflation Levels** (<h4>): Detail high inflation regions/countries (e.g., emerging markets) vs. low/stable (e.g., advanced economies), with trends (rising/falling/sticky) from articles.\n"
        "   - **Economic Phases by World Regions** (<h4>): Categorize phases for North America, Europe, Asia-Pacific, Latin America, Middle East/Africa, Emerging Markets.\n"
        "   - **Key Influences & Mechanisms** (<h4>): Explain US tariffs' impact on inflation/growth, benefits/drawbacks of lower oil prices (esp. for US), and other forces like monetary policy.\n"
        "   - **Deep Dive: Theory & Implications** (<h4>): Select 2-3 topics from articles. For each, explain underlying economic theory (e.g., supply/demand shocks, protectionism vs. comparative advantage) and implications for capital allocation/wealth preservation. Use .theory-box class for styling if needed, but keep simple.\n\n"

        "OUTPUT RULES:\n"
        "- Tone: Concise, objective, Wall Street analytical.\n"
        "- Factuality: Every claim must trace to specific articles; use phrases like 'per [source]' if needed.\n"
        "- Balance: Reflect source diversity without bias.\n"
        "- HTML: Plain <h3>/<h4>/<p>/<ul>/<li>; no images, no code blocks.\n\n"

        f"RAW INTEL:\n{raw_text}"
    )

    try:
        # Low temperature for factual, consistent output
        generation_config = {
            "temperature": 0.2,
            "top_p": 0.95,
            "top_k": 64,
            "max_output_tokens": 8192,
        }

        response = model.generate_content(
            prompt,
            generation_config=generation_config,
            safety_settings=safety_settings
        )
        
        # Clean HTML
        clean_html = response.text.strip().replace("```html", "").replace("```", "").strip()
        
        # Append transparent sources section with clickable links to top 30 articles
        sources_html = "<h3>Transparent Sources</h3><ul>"
        for a in articles[:30]:
            sources_html += f'<li><a href="{a["url"]}">{a["title"]} - {a["source"]["name"]} ({a["publishedAt"][:10]})</a></li>'
        sources_html += "</ul>"
        clean_html += sources_html
        
        return clean_html, None
    except Exception:
        error_msg = traceback.format_exc()
        return None, error_msg

def create_fallback_html(articles, error_msg):
    # Better fallback: cleaner, with links and descriptions
    html = f"<div style='background:#fee;padding:15px;border:1px solid #c00;'><h3>Analysis Failed</h3><pre>{error_msg}</pre></div><h4>Raw Articles (Top 30)</h4><ul>"
    for a in articles[:30]:
        desc = a.get('description', 'No description')
        html += f"<li><b><a href='{a['url']}'>{a['title']}</a></b><br><i>{a['source']['name']} - {a['publishedAt'][:10]}</i><br>{desc[:200]}...</li>"
    html += "</ul>"
    return html

def send_email(html_content, subject_prefix=""):
    print("--- Sending Email Report ---")
    try:
        with open('email_template.html', 'r') as f:
            template_str = f.read()
    except FileNotFoundError:
        # Provided HTML template for styling
        template_str = """
<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; color: #333; line-height: 1.6; max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #2c3e50; color: #ffffff; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .header h1 { margin: 0; font-size: 24px; }
        .date { font-size: 14px; opacity: 0.8; margin-top: 5px; }
        .section { background-color: #f9f9f9; padding: 15px; border-left: 5px solid #3498db; margin-bottom: 20px; border-radius: 4px; }
        .news-title { font-size: 18px; font-weight: bold; color: #2c3e50; margin-bottom: 5px; }
        .source-tag { font-size: 11px; background-color: #e0e0e0; color: #555; padding: 2px 6px; border-radius: 4px; text-transform: uppercase; margin-right: 5px; }
        /* The CSS for your Theory Box */
        .theory-box { background-color: #fff3cd; border: 1px solid #ffeeba; padding: 10px; font-size: 14px; margin-top: 10px; border-radius: 4px; color: #856404; }
        .theory-title { font-weight: bold; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; display: block; margin-bottom: 4px;}
        .footer { font-size: 12px; text-align: center; color: #888; margin-top: 30px; border-top: 1px solid #eee; padding-top: 20px; }
        a { color: #3498db; text-decoration: none; }
    </style>
</head>
<body>
    <div class="header">
        <h1>🌍 Global Intelligence Brief</h1>
        <div class="date">{{DATE}}</div>
    </div>
    <div style="padding: 20px 0;">
        {{CONTENT}}
    </div>
    <div class="footer">
        Automated Analysis by Gemini • 10-Source Global Monitor
    </div>
</body>
</html>
        """

    final_body = template_str.replace("{{DATE}}", datetime.now().strftime('%B %d, %Y').upper())
    final_body = final_body.replace("{{CONTENT}}", html_content)

    msg = MIMEMultipart('alternative')
    msg['From'] = EMAIL_USER
    msg['To'] = RECIPIENT_EMAIL
    msg['Subject'] = f"{subject_prefix} Global Macro Intelligence – {datetime.now().strftime('%Y-%m-%d')}"

    msg.attach(MIMEText(final_body, 'html'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print("Email delivered successfully.")
    except Exception as e:
        print(f"Email send failed: {e}")

if __name__ == "__main__":
    articles = fetch_global_news()
    if not articles:
        send_email("<p>No relevant macro news retrieved this period.</p>", "[NO DATA]")
    else:
        analysis_html, error_msg = analyze_news(articles)
        if analysis_html:
            send_email(analysis_html)
        else:
            send_email(create_fallback_html(articles, error_msg), "[ERROR]")