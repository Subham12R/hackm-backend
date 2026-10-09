from enum import StrEnum


class RoleCode(StrEnum):
    SUPER_ADMIN = "SUPER_ADMIN"
    EVENT_ADMIN = "EVENT_ADMIN"
    REGISTRATION_ADMIN = "REGISTRATION_ADMIN"
    EVALUATION_ADMIN = "EVALUATION_ADMIN"
    SUPPORT_ADMIN = "SUPPORT_ADMIN"
    JUDGE = "JUDGE"
    VIEWER = "VIEWER"
    PARTICIPANT = "PARTICIPANT"


ADMIN_PORTAL_ROLES = frozenset({
    RoleCode.SUPER_ADMIN,
    RoleCode.EVENT_ADMIN,
    RoleCode.REGISTRATION_ADMIN,
    RoleCode.EVALUATION_ADMIN,
    RoleCode.SUPPORT_ADMIN,
    RoleCode.JUDGE,
    RoleCode.VIEWER,
})
# Every staff role except judges, who only ever see anonymous projections.
ADMIN_READ_ROLES = ADMIN_PORTAL_ROLES - {RoleCode.JUDGE}
TEAM_REVIEW_ROLES = frozenset({RoleCode.SUPER_ADMIN, RoleCode.EVENT_ADMIN, RoleCode.REGISTRATION_ADMIN})
ASSIGNMENT_ROLES = frozenset({RoleCode.SUPER_ADMIN, RoleCode.EVENT_ADMIN, RoleCode.EVALUATION_ADMIN})
EVENT_CONTROL_ROLES = frozenset({RoleCode.SUPER_ADMIN, RoleCode.EVENT_ADMIN})
SUPPORT_ROLES = frozenset({RoleCode.SUPER_ADMIN, RoleCode.EVENT_ADMIN, RoleCode.SUPPORT_ADMIN})
MONEY_ROLES = frozenset({RoleCode.SUPER_ADMIN})
# Desk staff who mark arrivals at the finals venue.
CHECKIN_ROLES = frozenset({RoleCode.SUPER_ADMIN, RoleCode.EVENT_ADMIN, RoleCode.REGISTRATION_ADMIN, RoleCode.SUPPORT_ADMIN})
