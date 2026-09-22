"""POSITION boundary helpers: metres, x=Unity x, y=Unity z, heading CCW from +x."""


def unity_heading_to_engine(yaw_deg: float) -> float:
    """Unity +z/yaw=0 becomes engine +y/heading=90; yaw=90 becomes heading=0."""
    return (90.0 - yaw_deg) % 360.0
