from rest_framework.permissions import BasePermission


class IsPotentialQuestionBankAdmin(BasePermission):
    message = "You do not have permission to manage the Potential Question Bank."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and str(getattr(user, "employee_number", "")) == "100607"
        )