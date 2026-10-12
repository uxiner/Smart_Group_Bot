"""价格目录与计价（**不接触**数据库，只做纯计算）。

三件必须成立的事：

* **缓存不能重复计费**。网关把 ``cached_tokens`` 作为 ``prompt_tokens`` 的**子集**
  归一化上报，所以按 input 单价再算一遍缓存部分等于收两次钱。可计价的
  未缓存输入 = ``prompt_tokens - cached_tokens``。``cache_write`` 单独计
  （Anthropic 的 ``cache_creation_input_tokens`` 不含在 prompt 里），而
  ``thinking`` 属�� output 内，不再加一次。
* **缺单价 = 该部分金额未知**，不是 0。填 0 会让「没有计价配置」显示成
  「这次免费」，预算和报表就同时在说谎。
* **不同币种不能相加**。相加得到一个没有意义的数字比返回 null 更糟。
* **provider_key 是内部配置槽标识，绝不是 API Key**；这里也不出现任何
  endpoint URL（网关 URL 常带查询串密钥）。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any, Callable, Iterable

#: 金额一律用**整数微单位**（1e-6 计价货币单位）落库：浮点在累加与比较上
#: 迟早出错，而"预算差一分钱"这种偏差在按百万 token 计价的场景里是常态。
MICRO = Decimal("0.000001")
#: 保留这个常量是为了让「每百万 token」这个单位在代码里可见：
#: 换算时它与 micro 的百万分之一正好抵消，所以计算里不出现除号。
TOKENS_PER_PRICE_UNIT = Decimal("1000000")

CURRENCY_MAX_LENGTH = 8
COMPONENTS: tuple[str, ...] = (
    "input",
    "output",
    "cache_read",
    "cache_write",
)


class PricingError(ValueError):
    """价格目录或计价输入不合法。"""


def _decimal(raw: Any, *, field: str) -> Decimal | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, Decimal):
        value = raw
    else:
        text = str(raw).strip()
        if not text:
            return None
        try:
            value = Decimal(text)
        except (InvalidOperation, ValueError) as exc:
            raise PricingError(f"{field} 不是合法数字：{text}") from exc
    if not value.is_finite():
        raise PricingError(f"{field} 必须是有限数。")
    if value < 0:
        raise PricingError(f"{field} 不能为负。")
    return value


@dataclass(frozen=True, slots=True)
class PriceEntry:
    """一个 (provider_key, model) 的四段单价，单位都是「每百万 token」。"""

    provider_key: str
    model: str
    currency: str
    input_per_million: Any = None
    output_per_million: Any = None
    cache_read_per_million: Any = None
    cache_write_per_million: Any = None

    def __post_init__(self) -> None:
        provider = str(self.provider_key or "").strip().lower()
        model = str(self.model or "").strip()
        currency = str(self.currency or "").strip().upper()
        if not provider:
            raise PricingError("provider_key 不能为空。")
        if not model:
            raise PricingError("model 不能为空。")
        if not currency or len(currency) > CURRENCY_MAX_LENGTH:
            raise PricingError("currency 无效。")
        object.__setattr__(self, "provider_key", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "currency", currency)
        for field in (
            "input_per_million",
            "output_per_million",
            "cache_read_per_million",
            "cache_write_per_million",
        ):
            object.__setattr__(self, field, _decimal(getattr(self, field), field=field))

    def price_for(self, component: str) -> Decimal | None:
        return {
            "input": self.input_per_million,
            "output": self.output_per_million,
            "cache_read": self.cache_read_per_million,
            "cache_write": self.cache_write_per_million,
        }.get(component)

    def complete_for(self, components: Iterable[str]) -> bool:
        return all(self.price_for(component) is not None for component in components)

    def document(self) -> dict[str, Any]:
        return {
            "provider_key": self.provider_key,
            "model": self.model,
            "currency": self.currency,
            "input_per_million": (
                str(self.input_per_million) if self.input_per_million is not None else None
            ),
            "output_per_million": (
                str(self.output_per_million) if self.output_per_million is not None else None
            ),
            "cache_read_per_million": (
                str(self.cache_read_per_million)
                if self.cache_read_per_million is not None
                else None
            ),
            "cache_write_per_million": (
                str(self.cache_write_per_million)
                if self.cache_write_per_million is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class PriceCatalog:
    """一版价格目录。改价 = 新版本，历史账不追改。"""

    version: int
    entries: tuple[PriceEntry, ...] = ()

    def __post_init__(self) -> None:
        if int(self.version) < 1:
            raise PricingError("价格目录版本必须 ≥1。")
        object.__setattr__(self, "version", int(self.version))
        object.__setattr__(self, "entries", tuple(self.entries or ()))

    def find(self, *, provider_key: Any, model: Any) -> PriceEntry | None:
        provider = str(provider_key or "").strip().lower()
        name = str(model or "").strip()
        for entry in self.entries:
            if entry.provider_key == provider and entry.model == name:
                return entry
        return None

    def document(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "entries": [entry.document() for entry in self.entries],
        }


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """一次调用的金额估算。``micro is None`` = **未知**，不是 0。"""

    currency: str
    micro: int | None
    uncached_input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    unknown_components: tuple[str, ...] = ()
    price_version: int = 0

    @property
    def known(self) -> bool:
        return self.micro is not None

    def document(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "micro": self.micro,
            "estimated": self.known,
            "uncached_input_tokens": self.uncached_input_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "output_tokens": self.output_tokens,
            "unknown_components": list(self.unknown_components),
            "price_version": self.price_version,
        }


def _int_or_zero(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def estimate(
    entry: PriceEntry | None,
    *,
    prompt_tokens: Any = 0,
    output_tokens: Any = 0,
    cached_tokens: Any = 0,
    cache_write_tokens: Any = 0,
    price_version: int = 0,
) -> CostEstimate:
    """按单价算一次调用的金额。缺任何一段单价 → 该金额未知。"""

    prompt = _int_or_zero(prompt_tokens)
    output = _int_or_zero(output_tokens)
    cached = min(_int_or_zero(cached_tokens), prompt) if prompt else 0
    cache_write = _int_or_zero(cache_write_tokens)
    # 缓存命中是 prompt 的子集：未缓存部分才是 input 段。
    uncached = max(0, prompt - cached)

    if entry is None:
        return CostEstimate(
            currency="",
            micro=None,
            uncached_input_tokens=uncached,
            cache_read_tokens=cached,
            cache_write_tokens=cache_write,
            output_tokens=output,
            unknown_components=COMPONENTS,
            price_version=price_version,
        )

    totals: list[tuple[str, int, Decimal]] = []
    unknown: list[str] = []
    for component, tokens in (
        ("input", uncached),
        ("cache_read", cached),
        ("cache_write", cache_write),
        ("output", output),
    ):
        if tokens <= 0:
            continue
        unit = entry.price_for(component)
        if unit is None:
            unknown.append(component)
            continue
        totals.append((component, tokens, unit))

    if unknown:
        return CostEstimate(
            currency=entry.currency,
            micro=None,
            uncached_input_tokens=uncached,
            cache_read_tokens=cached,
            cache_write_tokens=cache_write,
            output_tokens=output,
            unknown_components=tuple(unknown),
            price_version=price_version,
        )

    # 单价单位是「每百万 token 的货币单位」，而 micro 是「百万分之一货币单位」，
    # 两个百万在换算里正好抵消：micro = tokens × price。
    # 写成 tokens × price / 1e6 会把整笔金额截断成 0（$0.00052 变成 0），于是
    # 预算看起来从来没被触达过 —— 正是"看起来在保护但其实没保护"。
    #
    # 不足 1 micro 的金额**向上取整到 1**：按 0.1/百万计价的模型，一次调用只有
    # 0.0002 micro，``int()`` 会把它抹成 0，于是这类模型的调用永远免费。
    # 报表侧可以接受这点误差，但硬预留必须宁可多算。
    micro_total = Decimal("0")
    for _component, tokens, unit in totals:
        micro_total += Decimal(tokens) * unit
    rounded = _ceil_micro(micro_total)
    return CostEstimate(
        currency=entry.currency,
        micro=rounded,
        uncached_input_tokens=uncached,
        cache_read_tokens=cached,
        cache_write_tokens=cache_write,
        output_tokens=output,
        unknown_components=(),
        price_version=price_version,
    )


def conservative_upper_bound(
    entry: PriceEntry | None,
    *,
    prompt_tokens: Any,
    max_output_tokens: Any,
) -> CostEstimate:
    """预留用**保守上界**：对可能发生的每一支都按最贵的单价算。

    两个必须覆盖的分支：

    * **缓存**：缓存单价可能比普通输入**更贵**（写入溢价是常态），所以未缓存
      输入那一支不能按 input 价算，必须取 ``max(input, cache_read, cache_write)``。
      曾经按 input 价预留、结算回 usage 发现全是缓存命中，实际金额直接翻十倍 ——
      所谓 hard cap 根本没成立。
    * **输出**：没有 ``max_tokens`` 上界时金额上界**不存在**，必须返回未知，
      而不是按 0 输出算出一个"看起来有上界"的值。

    ``max_output_tokens is None``（或非整数）返回 ``micro=None``；调用方据此
    拒绝放行，而不是当作 0 元。
    """

    prompt = _strict_count(prompt_tokens)
    if prompt is None:
        return CostEstimate(
            currency=entry.currency if entry else "",
            micro=None,
            uncached_input_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            output_tokens=0,
            unknown_components=("input_bound",),
        )
    if max_output_tokens is None or max_output_tokens == "":
        return CostEstimate(
            currency=entry.currency if entry else "",
            micro=None,
            uncached_input_tokens=prompt,
            cache_read_tokens=0,
            cache_write_tokens=0,
            output_tokens=0,
            unknown_components=("output_bound",),
        )
    output = _strict_count(max_output_tokens)
    if entry is None or output is None:
        return CostEstimate(
            currency=entry.currency if entry else "",
            micro=None,
            uncached_input_tokens=prompt,
            cache_read_tokens=0,
            cache_write_tokens=0,
            output_tokens=output or 0,
            unknown_components=("price",),
        )
    missing = [
        name
        for name in ("input", "cache_read", "cache_write", "output")
        if entry.price_for(name) is None
    ]
    if missing:
        return CostEstimate(
            currency=entry.currency,
            micro=None,
            uncached_input_tokens=prompt,
            cache_read_tokens=0,
            cache_write_tokens=0,
            output_tokens=output,
            unknown_components=tuple(missing),
        )
    input_prices = [
        entry.price_for("input"),
        entry.price_for("cache_read"),
        entry.price_for("cache_write"),
    ]
    worst_input = max(price for price in input_prices if price is not None)
    micro = Decimal(prompt) * worst_input + Decimal(output) * entry.price_for("output")
    return CostEstimate(
        currency=entry.currency,
        # **向上取整**。``int()`` 是向下截断：0.1/百万 的四段价配 prompt=1 会算成
        # 0 micro，于是零金额上限也被放行 —— 一个"永远不触发"的 hard cap 比没有
        # 更危险。1.9 micro 也必须记 2 而不是 1。
        micro=_ceil_micro(micro),
        uncached_input_tokens=prompt,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=output,
    )


def _ceil_micro(value: Decimal) -> int:
    """micro 金额向上取整（硬预留宁可多算，不可少算）。"""

    if value <= 0:
        return 0
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _strict_count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not value.is_integer() or value < 0:
            return None
        return int(value)
    return _int_or_zero(value) if value != "" else None


def estimate_for(
    catalog: PriceCatalog,
    *,
    provider_key: Any,
    model: Any,
    prompt_tokens: Any = 0,
    output_tokens: Any = 0,
    cached_tokens: Any = 0,
    cache_write_tokens: Any = 0,
) -> CostEstimate:
    entry = catalog.find(provider_key=provider_key, model=model)
    return estimate(
        entry,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        price_version=catalog.version,
    )


def sum_amounts(items: Iterable[CostEstimate]) -> int | None:
    """把同一币种的金额相加；出现未知或跨币种一律返回 ``None``。"""

    currencies: set[str] = set()
    total = 0
    seen = False
    for item in items:
        seen = True
        if not item.known:
            return None
        currencies.add(item.currency)
        total += int(item.micro or 0)
    if not seen or len(currencies) > 1:
        return None
    return total


# ---------------------------------------------------------------------------
# 发送时冻结：台账只在**发出请求的那一刻**取价
# ---------------------------------------------------------------------------
#
# 事后拿目录的**最新**版本重算历史，等于替客户改昨天的账单：同一群同一模型先
# 按 v1 的价发出、再按 v2 的价发出，两次请求在账上必须各自带着自己那一版。
# 所以 attempt 在**真正发出前**就把「当时目录版本 + 该 provider/model 的条目」
# 冻结下来，结算时只用这份快照；目录后来改成 v3 也不会动到已经记下的金额。
#
# 冻结对象是不可变的（``PriceFreeze`` 是 frozen dataclass，``PriceEntry`` 也是，
# 单价是不可变 ``Decimal``），所以之后无论怎么改目录或传入对象，都不会污染
# 在飞请求 —— 这就是"深复制"在这里的等价物，不需要真的 ``copy.deepcopy``。
#
# 取价**不查数据库**：source 是进程内目录的实时读取器（价格目录本来就在内存里
# 按版本更新）。目录没装配、读取抛异常、或者目录里没有这个 provider/model 时，
# 冻结的是"发送时就没有价格元数据"这个事实（``entry=None``）—— 它**不是** 0，
# 也**不会**被后来的补价回算。

#: 进程内价格目录的**实时**读取器。装配在启动与 HTTP 路由注册处完成。
_CATALOG_SOURCE: Callable[[], PriceCatalog | None] | None = None


def configure_catalog_source(
    source: Callable[[], PriceCatalog | None] | None,
) -> None:
    """装配"当前价格目录"的读取器（返回 ``None`` 等价于没有目录）。"""

    global _CATALOG_SOURCE
    _CATALOG_SOURCE = source


def catalog_source() -> Callable[[], PriceCatalog | None] | None:
    return _CATALOG_SOURCE


def current_catalog() -> PriceCatalog:
    """当前目录；没有装配 / 读取失败一律当作"空目录"，**不猜任何价格**。"""

    source = _CATALOG_SOURCE
    if source is None:
        return PriceCatalog(version=1)
    try:
        catalog = source()
    except Exception:  # noqa: BLE001 - 取不到价就是未知，不能让台账炸掉
        return PriceCatalog(version=1)
    if isinstance(catalog, PriceCatalog):
        return catalog
    return PriceCatalog(version=1)


@dataclass(frozen=True, slots=True)
class PriceFreeze:
    """一次 attempt 在**发送时**冻结的价格身份与条目。"""

    provider_key: str = ""
    model: str = ""
    #: 冻结时的目录版本；``0`` = 发送时根本没有价格元数据。
    price_version: int = 0
    #: 条目币种；``""`` = 发送时不知道币种（**不猜 USD**）。
    currency: str = ""
    entry: PriceEntry | None = None

    @property
    def has_entry(self) -> bool:
        return self.entry is not None

    def estimate(
        self,
        *,
        prompt_tokens: Any = 0,
        output_tokens: Any = 0,
        cached_tokens: Any = 0,
        cache_write_tokens: Any = 0,
    ) -> CostEstimate:
        """只按**这份冻结条目**计价（不回头查目录）。"""

        return estimate(
            self.entry,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            price_version=self.price_version,
        )

    def document(self) -> dict[str, Any]:
        return {
            "provider_key": self.provider_key,
            "model": self.model,
            "price_version": int(self.price_version),
            "currency": self.currency,
            "entry": self.entry.document() if self.entry is not None else None,
            "frozen": True,
        }


def freeze_price(provider_key: Any, model: Any) -> PriceFreeze:
    """按**当前**目录冻结一次 attempt 的价格身份。"""

    catalog = current_catalog()
    key = str(provider_key or "").strip().lower()
    name = str(model or "").strip()
    entry = catalog.find(provider_key=key, model=name)
    version = int(catalog.version)
    if entry is None:
        # 目录版本照样留下：它说明"这一版里没有这个 provider/model"，
        # 跟"当时没有目录"是两件事，报表要能分开讲。
        return PriceFreeze(
            provider_key=key,
            model=name,
            price_version=version if key and name else 0,
            currency="",
            entry=None,
        )
    return PriceFreeze(
        provider_key=key,
        model=name,
        price_version=version,
        currency=entry.currency,
        entry=entry,
    )