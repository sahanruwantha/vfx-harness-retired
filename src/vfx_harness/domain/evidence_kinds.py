"""Evidence-kind vocabulary shared by domain contracts and evidence adapters."""

PROJECTED_ORIGIN_KINDS = frozenset({"projected_origin_x", "projected_origin_y"})
# Registry-declared image instruments among the scene-contract kinds: they measure
# pixels and therefore owe raster, an optical-signal producer, and a rendered carrier
# exactly as image_contract debts do (HIR-0114, HIR-0176).
FUNCTIONAL_KINDS = frozenset({"control_render_response", "frame_delta", "render_region_stat"})
# The absolute pixel statistics among them: a black or tinted plate satisfies a band or a
# response sweep without any subject, so a required image claim over these rows is an
# image debt for the signal and subject bootstrap. `frame_delta` is relative between two
# frames and its payments are already refuted by the pre-unit adversary rule (HIR-0048).
PIXEL_STATISTIC_KINDS = frozenset({"control_render_response", "render_region_stat"})
