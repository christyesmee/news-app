import os
import requests
import smtplib
import traceback
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import google.generativeai as genai
from datetime import datetime, timedelta

# --- CONFIGURATION ---
# Ensure these are set in your environment variables
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

# BALANCED SOURCE LIST (Global, Financial, Geopolitical)
SOURCES = [
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com",  # Tier 1 Financial
    "apnews.com", "bbc.com", "economist.com",             # Western Major
    "scmp.com", "asia.nikkei.com", "thehindu.com",        # Asian Perspectives
    "dw.com", "france24.com",                             # European Perspectives
    "aljazeera.com", "arabnews.com"                       # Middle East Perspectives
]

def get_working_model():
    """Configures Gemini and finds the best available model."""
    genai.configure(api_key=GEMINI_API_KEY)
    # Priority list of models
    preferred = ['gemini-1.5-pro-latest', 'gemini-1.5-pro', 'gemini-1.5-flash-latest', 'gemini-1.5-flash']
    try:
        available = [m.name.replace("models/", "") for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        for p in preferred:
            if p in available:
                return p
        return available[0] if available else None
    except Exception as e:
        print(f"Error listing models: {e}")
        return None

def fetch_global_news():
    """Fetches high-impact news from the last 2 days to ensure freshness."""
    print("--- Fetching Global News ---")
    domains = ",".join(SOURCES)
    # Reduced to 2 days for higher relevance/immediacy
    date_from = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
    
    # Query logic: Focus on macro factors
    query = "(economy OR inflation OR 'central bank' OR supply chain OR 'semiconductor' OR energy OR 'foreign policy' OR 'trade agreement')"
    
    url = (
        f"https://newsapi.org/v2/everything?"
        f"q={query}&"
        f"domains={domains}&"
        f"from={date_from}&"
        f"sortBy=popularity&"  # Sort by popularity to get the "big" stories first
        f"language=en&"
        f"pageSize=80&"
        f"apiKey={NEWS_API_KEY}"
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
    """Sends articles to Gemini for strategic analysis."""
    model_name = get_working_model()
    if not model_name:
        return None, "No Gemini model available."

    print(f"--- Analyzing with {model_name} ---")
    
    # Prepare text for context window
    raw_text = ""
    for i, a in enumerate(articles[:50]): # Limit to top 50 to fit context context comfortably
        desc = a.get('description') or "No description"
        raw_text += (
            f"Article {i+1} | Source: {a['source']['name']} | Date: {a['publishedAt'][:10]}\n"
            f"Title: {a['title']}\n"
            f"Summary: {desc}\nURL: {a['url']}\n---\n"
        )

    model = genai.GenerativeModel(model_name)
    
    safety_settings = [{"category": c, "threshold": "BLOCK_NONE"} for c in [
        "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT"
    ]]

    # --- PROMPT ENGINEERING ---
    prompt = (
        "Role: You are a macro-economic strategy engine. You provide cold, factual, high-level analysis.\n"
        "Input: A list of news articles from various global sources.\n"
        "Task: Synthesize this data into an HTML briefing. \n\n"

        "**STRICT GUIDELINES:**\n"
        "1. NO greeting, NO fluff, NO 'as an aspiring billionaire'. Start directly with the analysis.\n"
        "2. ENSURE SOURCE VARIETY. Do not rely on a single news outlet. Cite different sources.\n"
        "3. Tone: Professional, dense, executive-level English.\n\n"

        "**OUTPUT STRUCTURE (HTML Only):**\n\n"

        "\n"
        "<h2>Global Macro Synthesis</h2>\n"
        "<p><i>A 150-word high-level synthesis of the current global state, connecting dots between regions (e.g., how EU policy affects Asian markets).</i></p>\n\n"

        "\n"
        "<h2>Strategic Deep Dives</h2>\n"
        "<p>Select the top 3 most critical concepts relevant to today's news (e.g., Asymmetric Risk, Second-Order Effects, Capital Allocation, The Thucydides Trap, Creative Destruction). Use the CSS class 'deep-dive-box' for these.</p>\n"
        
        "<div class='deep-dive-box'>\n"
        "  <h3>1. [Insert Concept Name]</h3>\n"
        "  <p><strong>Theory:</strong> [Brief definition of the mental model/economic law].</p>\n"
        "  <p><strong>Current Application:</strong> [How specifically does today's news demonstrate this concept? Cite specific events].</p>\n"
        "  <p><strong>Strategic Implication:</strong> [What is the leverage point? How does one capitalize on this?].</p>\n"
        "</div>\n"
        "\n\n"

        "\n"
        "<h2>Critical Developments</h2>\n"
        "\n"
        "<div class='section'>\n"
        "  <p class='news-title'>[Event Headline]</p>\n"
        "  <p>[2-sentence factual summary]. <strong>Impact:</strong> [Why it matters].</p>\n"
        "  <p style='font-size:12px'><a href='[URL]'>Read Source ([Source Name])</a></p>\n"
        "</div>\n\n"

        f"DATA SOURCE:\n{raw_text}"
    )

    try:
        generation_config = {
            "temperature": 0.3, # Low temperature for factual consistency
            "max_output_tokens": 8192,
        }

        response = model.generate_content(prompt, generation_config=generation_config, safety_settings=safety_settings)
        clean_html = response.text.strip().replace("```html", "").replace("```", "").strip()

        # Append source list at the bottom
        sources_html = "<h3>Reference Links</h3><ul style='font-size:12px; color:#666;'>"
        # Deduplicate links for the footer
        seen_urls = set()
        count = 0
        for a in articles:
            if a['url'] not in seen_urls and count < 15:
                sources_html += f"<li><a href='{a['url']}'>{a['title']}</a> ({a['source']['name']})</li>"
                seen_urls.add(a['url'])
                count += 1
        sources_html += "</ul>"
        
        return clean_html + sources_html, None
    except Exception:
        return None, traceback.format_exc()

def create_fallback_html(articles, error):
    html = f"<div style='border:1px solid red; padding:20px;'><h3>Analysis Generation Failed</h3><pre>{error}</pre></div>"
    html += "<h3>Raw Feed</h3><ul>"
    for a in articles[:10]:
        html += f"<li><a href='{a['url']}'>{a['title']}</a> - {a['source']['name']}</li>"
    html += "</ul>"
    return html

def send_email(html_content, prefix=""):
    print("--- Sending Email ---")
    
    # Read external HTML template
    try:
        with open('email_template.html', 'r', encoding='utf-8') as f:
            template = f.read()
    except FileNotFoundError:
        print("Error: email_template.html not found.")
        return

    # Insert Content and Date
    current_date = datetime.now().strftime('%d %B %Y')
    body = template.replace("{{CONTENT}}", html_content)
    body = body.replace("{{DATE}}", current_date)

    msg = MIMEMultipart('alternative')
    msg['From'] = EMAIL_USER
    msg['To'] = RECIPIENT_EMAIL
    msg['Subject'] = f"{prefix} Strategic Intelligence: {current_date}"

    msg.attach(MIMEText(body, 'html'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print("Email sent successfully.")
    except Exception as e:
        print(f"Email failed: {e}")

if __name__ == "__main__":
    articles = fetch_global_news()
    if not articles:
        print("No articles found to analyze.")
    else:
        html_analysis, err = analyze_news(articles)
        if html_analysis:
            send_email(html_analysis)
        else:
            send_email(create_fallback_html(articles, err), "[ERROR]")