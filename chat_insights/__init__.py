"""
Chat Insights -- weekly analysis of Chatbase conversations.

Pulls the week's conversations from Chatbase, works out what happened in them,
and emails a manager a report: questions the bot failed to answer, recurring
themes, sales leads, volume, and unhappy customers.

Same shape as content_seo_agent: everything that can be computed
deterministically is, and the model is only asked for the parts that genuinely
need judgement.
"""
