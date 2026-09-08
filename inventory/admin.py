from django.contrib import admin

from .models import Asset, CheckOut, Employee, OverdueNotice


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("asset_tag", "name", "category", "status", "purchase_date")
    search_fields = ("asset_tag", "name")
    list_filter = ("category", "status")


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("employee_code", "full_name", "email", "is_active")
    search_fields = ("employee_code", "full_name", "email")
    list_filter = ("is_active",)


@admin.register(CheckOut)
class CheckOutAdmin(admin.ModelAdmin):
    list_display = ("asset", "employee", "checked_out_at", "due_at", "returned_at")
    list_select_related = ("asset", "employee")


@admin.register(OverdueNotice)
class OverdueNoticeAdmin(admin.ModelAdmin):
    list_display = ("checkout", "notice_date", "created_at")
    list_select_related = ("checkout",)
