"""CLI 错误分类：可预期业务错误 → 退出码 1。"""


class BusinessError(Exception):
    """可预期的业务失败（参数缺失、图号重复、无此商品等），CLI 退出码 1。"""
