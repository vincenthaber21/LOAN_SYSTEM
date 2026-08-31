# Lumen Lending

Lumen Lending is a server-rendered Django loan management system for borrowers and loan officers. Borrowers can estimate and submit loans, track approvals, view repayment schedules, and record payments. Officers can review applications, approve final terms, disburse funds, manage borrowers, and export portfolio reports.

## Run locally or on Replit

```bash
python manage.py migrate
python manage.py seed_demo
python manage.py runserver 0.0.0.0:$PORT
```

The Replit run button starts the same Django server through the app workflow. Development uses SQLite by default. Set `DATABASE_URL` to a PostgreSQL URL to switch databases; `dj-database-url` handles the connection.

## Demo accounts

| Role | Email | Password |
| --- | --- | --- |
| Loan officer | `officer@lumen.test` | `Officer123!` |
| Borrower | `maria@lumen.test` | `Borrower123!` |
| Borrower | `carlo@lumen.test` | `Borrower123!` |

## Main workflow

`draft → submitted → under review → approved/rejected → active → closed`

An approved application becomes a loan when an officer confirms disbursement. Disbursement creates the full amortized installment schedule. Payments can be applied to a selected installment or automatically to the next unpaid installments.

## Project structure

- `loan_system/` — Django settings, URL configuration, and WSGI entrypoint
- `lending/models.py` — users, loan products, applications, loans, installments, payments, and documents
- `lending/services.py` — amortization, schedule generation, disbursement, payment allocation, and overdue handling
- `lending/views.py` — borrower and officer screens
- `templates/` and `static/` — server-rendered UI and custom styling

Create an administrator manually with `python manage.py createsuperuser`, or use the seeded officer account for the custom officer workspace.