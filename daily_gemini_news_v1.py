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

# 10 Diverse International Sources
SOURCES = [
    "reuters.com", "aljazeera.com", "bbc.co.uk", "scmp.com", 
    "dw.com", "bloomberg.com", "wsj.com", "theguardian.com", 
    "france24.com", "thehindu.com"
]

def get_working_model():
    """Auto-selects the best available Gemini model."""
    print("--- Selecting Best Available AI Model ---")
    genai.configure(api_key=GEMINI_API_KEY)
    try:
        all_models = [m.name.replace("models/", "") for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        
        # Priority: Pro (Deep reasoning) -> Flash (Fast/Reliable)
        for m in all_models:
            if "1.5-pro" in m: return m
        for m in all_models:
            if "1.5-flash" in m: return m
            
        return all_models[0] if all_models else None
    except Exception as e:
        print(f"!! Model error: {e}")
        return None

def fetch_global_news():
    print("--- 1. Fetching World News ---")
    domains = ",".join(SOURCES)
    date_from = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
    
    # Query for high-impact macro topics
    url = (
        f"https://newsapi.org/v2/everything?"
        f"q=(economy OR politics OR 'central bank' OR 'supply chain' OR geopolitics OR 'trade war')&"
        f"domains={domains}&"
        f"from={date_from}&"
        f"sortBy=publishedAt&"
        f"language=en&"
        f"pageSize=25&" # Higher volume for better synthesis
        f"apiKey={NEWS_API_KEY}"
    )
    
    try:
        response = requests.get(url)
        data = response.json()
        articles = data.get("articles", [])
        print(f"-> Found {len(articles)} articles.")
        return articles
    except Exception as e:
        print(f"!! Error fetching news: {e}")
        return []

def analyze_news(articles):
    model_name = get_working_model()
    if not model_name: return None, "No models available."

    print(f"--- 2. Analyzing with {model_name} ---")
    
    raw_text = ""
    for i, a in enumerate(articles[:20]):
        raw_text += f"{i+1}. {a['title']} - Source: {a['source']['name']} ({a['publishedAt'][:10]})\n"

    model = genai.GenerativeModel(model_name)
    
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    # --- THE BILLIONAIRE INTELLIGENCE PROMPT ---
    prompt = (
        "You are a strategic intelligence analyst. Your client is an aspiring billionaire investor who needs strictly factual, high-level analysis to allocate time, capital, and leverage effectively. "
        "Do NOT use conversational fillers (no 'Hello', no 'Here is your report'). "
        "Use the provided news headlines to generate a Strategic World Report in HTML format.\n\n"
        
        "### STRUCTURE REQUIREMENTS:\n"
        
        "1. **Key Political Developments** (<h3>)\n"
        "- Synthesize the most critical political events of the week. Focus on power shifts, conflicts, and policy changes.\n"
        
        "2. **Economic Outlook** (<h3>)\n"
        "- Summarize global market forecasts, GDP trends, and major corporate moves. Focus on growth vs. stagnation signals.\n"
        
        "3. **Interpretation** (<h3>) - *Teach the client how to read the world.*\n"
        "   - **Economic Cycles Interpretation** (<h4>): Analyze where the global economy is in the cycle (Early, Mid, Late, Recession) based on the news. Mention specific indicators. Insert an image tag:  where relevant.\n"
        "   - **Global Inflation Levels** (<h4>): Provide data/estimates on inflation trends (Rising/Falling/Sticky) across key regions.\n"
        "   - **Economic Phases by World Regions** (<h4>): Briefly break down the cycle phase for North America, Europe, Asia, and Emerging Markets.\n"
        
        "   - **Deep Dive: Theory & Rationale** (<h4>): Select 2 specific topics from the news (e.g., a specific tariff, a central bank move, a commodity spike). Explain the **Economic Theory** behind it. (e.g., if news is about Tariffs, explain 'Protectionism vs. Free Trade efficiency'; if about Oil, explain 'Supply Shock Elasticity'). Explain why this matters for wealth accumulation.\n"
        "     *Insert relevant image tags for these concepts, e.g., 

[Image of supply and demand curve shift]
 or 

[Image of bond yield curve]
.*\n"
        
        "OUTPUT RULES:\n"
        "- Tone: Professional, Wall Street Research, Educational.\n"
        "- Format: HTML (use <h3>, <h4>, <p>, <ul>, <li>).\n"
        "- **Constraint:** Only use facts present in the provided text or general economic knowledge to explain those facts. Do not hallucinate events.\n\n"
        
        f"RAW INTEL:\n{raw_text}"
    )

    try:
        response = model.generate_content(prompt, safety_settings=safety_settings)
        clean_html = response.text.replace("```html", "").replace("```", "")
        return clean_html, None
    except Exception:
        error_msg = traceback.format_exc()
        return None, error_msg

def create_fallback_html(articles, error_msg):
    html = f"<div style='background:#fee;padding:10px;'><h3>⚠️ Analysis Failed</h3><pre>{error_msg}</pre></div><ul>"
    for a in articles:
        html += f"<li><b>{a['title']}</b><br><i>{a['source']['name']}</i></li>"
    html += "</ul>"
    return html

def send_email(html_content, subject_prefix=""):
    print("--- 3. Sending Email ---")
    try:
        with open('email_template.html', 'r') as f:
            template_str = f.read()
    except:
        # Minimalist Template for the Billionaire Brief
        template_str = """
        <html>
        <body style="font-family: Georgia, serif; color: #111; max-width: 800px; margin: auto; padding: 20px;">
            <div style="border-bottom: 2px solid #000; padding-bottom: 10px; margin-bottom: 20px;">
                <h1 style="margin:0; font-size: 28px; text-transform: uppercase;">Strategic Intelligence Brief</h1>
                <div style="font-size: 12px; color: #555; margin-top: 5px;">{{DATE}} | GLOBAL MACRO & GEOPOLITICS</div>
            </div>
            {{CONTENT}}
            <div style="margin-top: 40px; border-top: 1px solid #ccc; padding-top: 10px; font-size: 10px; color: #777;">
                Generated by Gemini 1.5 • Investment Research Logic
            </div>
        </body>
        </html>
        """

    final_body = template_str.replace("{{DATE}}", datetime.now().strftime('%B %d, %Y').upper())
    final_body = final_body.replace("{{CONTENT}}", html_content)

    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = RECIPIENT_EMAIL
    msg['Subject'] = f"{subject_prefix} 📈 Macro Intelligence: {datetime.now().strftime('%Y-%m-%d')}"
    msg.attach(MIMEText(final_body, 'html'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print("vv Email sent successfully!")
    except Exception as e:
        print(f"!! Failed to send email: {e}")

if __name__ == "__main__":
    articles = fetch_global_news()
    if not articles:
        send_email("<p>No significant market news found today.</p>", "[EMPTY]")
    else:
        analysis_html, error_msg = analyze_news(articles)
        if analysis_html:
            send_email(analysis_html)
        else:
            send_email(create_fallback_html(articles, error_msg), "[DEBUG]")