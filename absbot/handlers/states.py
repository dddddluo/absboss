from aiogram.fsm.state import State, StatesGroup


class UserStates(StatesGroup):
    create_username = State()
    redeem_code = State()
    bind_credentials = State()
    rebind_credentials = State()


class AdminStates(StatesGroup):
    registration_slots = State()
    server_lines = State()
    checkin_points = State()
    code_payload = State()
    points_delta = State()
    expiry_delta = State()
    unban_cost = State()


class SetupStates(StatesGroup):
    main_group = State()
    register_days = State()
    server_lines = State()
    checkin = State()
    active_retention = State()
    points_renewal = State()
    panel_photo = State()
    rebind_review_chat = State()
    disabled_delete = State()
