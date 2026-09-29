"""
The staff assistant: ask a question about BC Sands products, get an answer
built from what this system already knows.

Admin-only, and deliberately grounded. The fine-tuned model writes in the
house voice but does not hold a reliable catalogue in its head -- asked from
memory it called paving sand "a recycled aggregate made from crushed concrete",
and invented "10mm and 13mm sand". So nothing here asks the model what it
knows. Every answer is built from two sources it is handed at question time:

    the approved descriptions   written by the agent, checked by a person
    the Odoo product list       every active product's name and SKU

and the model is told to answer only from those, and to say when they do not
cover the question.
"""
