//
// Small Axon convenience wrapper around Haxall's own hisRead, for the
// batch-across-many-points case: readAll(point).hisReadAll("today")
// instead of hand-building the {id:...} rows + addMeta({range:...})
// request Grid hisRead(Grid) expects.
//
// Not named "hisRead" itself - Axon's function-name resolution when two
// libs define the same name isn't a verified-safe thing to bank on (same
// class of risk as the "his" vs "hx.hxd.his" lib-naming collision this
// pod already works around elsewhere), and hisRead is heavily used core
// Haxall infrastructure, not something worth risking a guess on.
//
// Delegates to hxApi::PhApiFuncs.hisRead - the exact same batch-read
// code path already proven live (one v{i} column per point, aligned by
// ts), just building its request Grid for the caller.
//

using xeto
using haystack
using axon
using hxApi

const class AxonSugar
{
  @Api @Axon
  static Grid hisReadAll(Grid pts, Str range)
  {
    rows := Dict[,]
    pts.each |Dict pt| { rows.add(Etc.dict1("id", pt->id)) }
    req := Etc.makeDictsGrid(["range": range], rows)
    return PhApiFuncs.hisRead(req)
  }
}
