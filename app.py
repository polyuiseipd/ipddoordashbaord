from flask import Flask, render_template, jsonify
import datetime
import os
import pytz
from icalendar import Calendar
from dateutil import rrule
import requests
from io import StringIO
import re
import time
import threading

app = Flask(__name__)

# Configuration
CALENDAR_URL = "https://outlook.office365.com/owa/calendar/acce68cede284051a60297253e2e11f6@polyu.edu.hk/cc2c548b8b5d4176b0c5949406bfd81717213170944509749253/calendar.ics"
CACHE_FILE = "calendar_cache.ics"
CACHE_EXPIRY = 10 * 60  # 10 minutes in seconds

# Global variable to store the cached calendar data
calendar_cache = {
    "data": None,
    "timestamp": 0
}

def update_calendar_cache():
    """
    Function to periodically update the calendar cache
    """
    while True:
        try:
            print("Updating calendar cache...")
            response = requests.get(CALENDAR_URL)
            response.raise_for_status()
            
            with app.app_context():
                calendar_cache["data"] = response.text
                calendar_cache["timestamp"] = time.time()
                
                # Also save to file for persistence
                with open(CACHE_FILE, 'w', encoding='utf-8') as f:
                    f.write(response.text)
                
            print(f"Calendar cache updated at {datetime.datetime.now()}")
        except Exception as e:
            print(f"Error updating calendar cache: {e}")
        
        # Sleep for 10 minutes
        time.sleep(CACHE_EXPIRY)

def get_calendar_data():
    """
    Get calendar data from cache or file
    """
    # First check memory cache
    if calendar_cache["data"] is not None and time.time() - calendar_cache["timestamp"] < CACHE_EXPIRY:
        return calendar_cache["data"]
    
    # Then check file cache
    if os.path.exists(CACHE_FILE):
        file_mod_time = os.path.getmtime(CACHE_FILE)
        if time.time() - file_mod_time < CACHE_EXPIRY * 2:  # Give a bit more leeway for file cache
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                calendar_cache["data"] = f.read()
                calendar_cache["timestamp"] = time.time()
                return calendar_cache["data"]
    
    # If we get here, need to fetch fresh data
    try:
        response = requests.get(CALENDAR_URL)
        response.raise_for_status()
        
        calendar_cache["data"] = response.text
        calendar_cache["timestamp"] = time.time()
        
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            f.write(response.text)
            
        return calendar_cache["data"]
    except Exception as e:
        print(f"Error fetching calendar: {e}")
        
        # Last resort - use existing file cache regardless of age
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return f.read()
        
        return ""

def parse_calendar_data(calendar_text):
    """
    Parse the iCalendar data and return events
    """
    try:
        cal = Calendar.from_ical(calendar_text)
        
        # Current date for checking if event is today
        hk_timezone = pytz.timezone('Asia/Hong_Kong')
        now = datetime.datetime.now(hk_timezone)
        today = now.date()
        
        events = []
        
        for component in cal.walk():
            if component.name == "VEVENT":
                # Get event details
                summary = str(component.get('summary', 'No Title'))
                description = str(component.get('description', ''))
                location = str(component.get('location', ''))
                status = str(component.get('status', ''))
                transp = str(component.get('transp', ''))
                busy_status = "FREE" if transp.upper() == "TRANSPARENT" else "BUSY"
                
                # Get Microsoft-specific properties if available
                ms_busy_status = None
                for prop_name in component:
                    if prop_name.startswith('X-MICROSOFT-CDO-BUSYSTATUS'):
                        ms_busy_status = str(component.get(prop_name))
                
                # Get start and end time
                start = component.get('dtstart').dt
                end = component.get('dtend').dt
                
                # Check if it's an all-day event
                all_day = False
                if isinstance(start, datetime.date) and not isinstance(start, datetime.datetime):
                    all_day = True
                
                # Skip all-day events for display in the timeline
                if all_day:
                    continue
                
                # Convert to datetime with timezone if needed
                if not isinstance(start, datetime.datetime):
                    start = datetime.datetime.combine(start, datetime.time.min)
                if not isinstance(end, datetime.datetime):
                    end = datetime.datetime.combine(end, datetime.time.max)
                
                # Handle timezone
                if start.tzinfo is None:
                    start = hk_timezone.localize(start)
                if end.tzinfo is None:
                    end = hk_timezone.localize(end)
                
                # Process recurring events
                rrule_str = component.get('rrule')
                if rrule_str:
                    # Get the recurrence rule
                    recur_rule = rrule_str.to_ical().decode('utf-8')
                    
                    # Convert to rrule object
                    rule_params = {}
                    for part in recur_rule.split(';'):
                        if '=' in part:
                            key, value = part.split('=')
                            if key == 'FREQ':
                                if value == 'WEEKLY':
                                    rule_params['freq'] = rrule.WEEKLY
                                elif value == 'DAILY':
                                    rule_params['freq'] = rrule.DAILY
                                elif value == 'MONTHLY':
                                    rule_params['freq'] = rrule.MONTHLY
                            elif key == 'UNTIL':
                                until_str = re.sub(r'Z$', '', value)
                                try:
                                    until_date = datetime.datetime.strptime(until_str, '%Y%m%dT%H%M%S')
                                    rule_params['until'] = until_date.replace(tzinfo=pytz.UTC)
                                except ValueError:
                                    # Handle date only format
                                    until_date = datetime.datetime.strptime(until_str[:8], '%Y%m%d')
                                    rule_params['until'] = until_date.replace(tzinfo=pytz.UTC)
                            elif key == 'INTERVAL':
                                rule_params['interval'] = int(value)
                            elif key == 'BYDAY':
                                byweekday = []
                                for day in value.split(','):
                                    day_map = {'MO': rrule.MO, 'TU': rrule.TU, 'WE': rrule.WE, 
                                              'TH': rrule.TH, 'FR': rrule.FR, 'SA': rrule.SA, 'SU': rrule.SU}
                                    if day in day_map:
                                        byweekday.append(day_map[day])
                                if byweekday:
                                    rule_params['byweekday'] = byweekday
                    
                    # Get occurrences for the next 7 days
                    if rule_params:
                        rule = rrule.rrule(dtstart=start, **rule_params)
                        
                        # Define date range for occurrences (today to next 7 days)
                        occurrences = rule.between(
                            now.replace(hour=0, minute=0, second=0, microsecond=0),
                            now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=7),
                            inc=True
                        )
                        
                        for occurrence in occurrences:
                            if not occurrence.tzinfo:
                                occurrence = pytz.UTC.localize(occurrence)
                            occurrence_local = occurrence.astimezone(hk_timezone)
                            
                            # Calculate the end time for this occurrence
                            time_delta = end - start
                            occurrence_end = occurrence_local + time_delta
                            
                            # Determine busy status - respect MS specific property if available
                            is_busy = True
                            if ms_busy_status and ms_busy_status.upper() == "FREE":
                                is_busy = False
                            elif transp.upper() == "TRANSPARENT":
                                is_busy = False
                            
                            # Add to events list
                            events.append({
                                "title": summary,
                                "description": description,
                                "location": location,
                                "start": occurrence_local.strftime("%H:%M"),
                                "end": occurrence_end.strftime("%H:%M"),
                                "date": occurrence_local.strftime("%Y-%m-%d"),
                                "is_busy": is_busy,
                                # Include precise times for accurate timeline positioning
                                "start_hour": occurrence_local.hour,
                                "start_minute": occurrence_local.minute,
                                "end_hour": occurrence_end.hour,
                                "end_minute": occurrence_end.minute
                            })
                else:
                    # Regular (non-recurring) event
                    start_local = start.astimezone(hk_timezone)
                    end_local = end.astimezone(hk_timezone)
                    event_date = start_local.date()
                    
                    # Only include events for today and the next 7 days
                    if today <= event_date <= today + datetime.timedelta(days=7):
                        # Determine busy status - respect MS specific property if available
                        is_busy = True
                        if ms_busy_status and ms_busy_status.upper() == "FREE":
                            is_busy = False
                        elif transp.upper() == "TRANSPARENT":
                            is_busy = False
                        
                        events.append({
                            "title": summary,
                            "description": description,
                            "location": location,
                            "start": start_local.strftime("%H:%M"),
                            "end": end_local.strftime("%H:%M"),
                            "date": event_date.strftime("%Y-%m-%d"),
                            "is_busy": is_busy,
                            # Include precise times for accurate timeline positioning
                            "start_hour": start_local.hour,
                            "start_minute": start_local.minute,
                            "end_hour": end_local.hour,
                            "end_minute": end_local.minute
                        })
        
        return events
        
    except Exception as e:
        print(f"Error parsing calendar: {e}")
        return []

# Route to serve the main page
@app.route('/')
def index():
    return render_template('index.html')

# API endpoint to get status and events
@app.route('/status')
def status():
    # Get current time in Hong Kong timezone
    hk_timezone = pytz.timezone('Asia/Hong_Kong')
    now = datetime.datetime.now(hk_timezone)
    
    # Format current time
    current_hour = now.hour
    current_minute = now.minute
    current_second = now.second
    current_date = now.strftime('%Y-%m-%d')
    
    # Fetch and parse calendar data
    calendar_text = get_calendar_data()
    all_events = parse_calendar_data(calendar_text)
    
    # Filter events for today
    today_events = [event for event in all_events if event['date'] == current_date]
    
    # Sort events by start time
    today_events.sort(key=lambda x: (x['start_hour'], x['start_minute']))
    
    # Calculate current time as decimal for comparison
    current_time_decimal = current_hour + (current_minute / 60)
    
    # Check if we're in an active event
    status_message = "No More Events Today"
    lab_status = "Available"
    color = "green"
    
    # Iterate through today's events to determine status
    for event in today_events:
        start_hour = event['start_hour']
        start_minute = event['start_minute']
        end_hour = event['end_hour']
        end_minute = event['end_minute']
        
        start_decimal = start_hour + (start_minute / 60)
        end_decimal = end_hour + (end_minute / 60)
        
        # If current time is within an event
        if start_decimal <= current_time_decimal < end_decimal:
            # Check if event is marked as busy
            if event['is_busy']:
                lab_status = "Occupied"
                color = "red"
                # Calculate remaining time
                remaining_minutes = int((end_decimal - current_time_decimal) * 60)
                if remaining_minutes < 60:
                    status_message = f"Ends in {remaining_minutes} minutes"
                else:
                    hours = remaining_minutes // 60
                    minutes = remaining_minutes % 60
                    status_message = f"Ends in {hours}h {minutes}m"
                break
        
        # If there's an upcoming event
        if current_time_decimal < start_decimal:
            # Check if event is marked as busy
            if event['is_busy']:
                minutes_until_start = int((start_decimal - current_time_decimal) * 60)
                if minutes_until_start < 60:  # If less than an hour away
                    status_message = f"Next event in {minutes_until_start} minutes"
                else:
                    hours = minutes_until_start // 60
                    minutes = minutes_until_start % 60
                    status_message = f"Next event in {hours}h {minutes}m"
                break
    
    # Return JSON response
    return jsonify({
        'status': lab_status,
        'message': status_message,
        'color': color,
        'events': today_events,
        'current_hour': current_hour,
        'current_minute': current_minute,
        'current_second': current_second
    })

if __name__ == '__main__':
    # Create necessary templates directory
    os.makedirs('templates', exist_ok=True)
    os.makedirs('static/images', exist_ok=True)
    
    # Start the background thread for updating calendar data
    update_thread = threading.Thread(target=update_calendar_cache, daemon=True)
    update_thread.start()
    
    # Start Flask app
    app.run(debug=True, host='0.0.0.0', port=5000)