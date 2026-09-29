"""
Site monitor: checks the online shop and emails when it has a problem.

    probe   one measured HTTP request, through this server's DNS or public DNS
    rules   turns a run of check results into incidents and notifications
    store   SQLite: checks, incidents, held notifications, state, settings
    emails  the alert, reminder, recovery, certificate and summary emails
    run     the job the BCSands-SiteMonitor task runs every five minutes
"""
