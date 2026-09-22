from django.urls import path

from . import views

urlpatterns = [
    # Member savings
    path("savings/", views.savings_dashboard, name="savings_dashboard"),
    path("savings/open/", views.savings_open_account, name="savings_open_account"),
    path("savings/<int:account_id>/", views.savings_account_detail, name="savings_account_detail"),
    path("savings/<int:account_id>/deposit/", views.savings_deposit, name="savings_deposit"),
    path("savings/<int:account_id>/withdraw/", views.savings_withdraw, name="savings_withdraw"),
    # Officer savings
    path("officer/savings/", views.officer_savings_accounts, name="officer_savings_accounts"),
    path("officer/savings/interest-export/", views.officer_export_savings_interest, name="officer_export_savings_interest"),
    path("officer/savings/open/", views.officer_open_savings_account, name="officer_open_savings_account"),
    path("officer/savings/available-products/", views.officer_available_savings_products, name="officer_available_savings_products"),
    path("officer/savings/<int:account_id>/", views.officer_savings_account_detail, name="officer_savings_account_detail"),
    path("officer/savings/<int:account_id>/edit/", views.officer_edit_savings_account, name="officer_edit_savings_account"),
    path("officer/savings/<int:account_id>/delete/", views.officer_delete_savings_account, name="officer_delete_savings_account"),
    path("officer/savings/products/", views.officer_savings_products, name="officer_savings_products"),
    path("officer/savings/products/add/", views.officer_add_savings_product, name="officer_add_savings_product"),
    path("officer/savings/products/<int:product_id>/edit/", views.officer_edit_savings_product, name="officer_edit_savings_product"),
]
