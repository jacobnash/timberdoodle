#! /usr/bin/env fan
//
// Self-check for the alphabetical IHisExt tie-break tdHis depends on to
// become Haxall's active historian (see build.fan's ponytail comment and
// docs-site/pages/haxall-drop-in.mdx, "A real, load-bearing naming trick").
// No live Haxall needed - this is the exact string comparison
// RuntimeExts.getByType makes. Run: `fan fantom-his-ext/test_his_naming.fan`
//
// Keep "his" below in sync with build.fan's index["xeto.bindings"] value -
// that's the actual lib name Haxall sorts, this just isn't able to read
// build.fan's own source to check it automatically.
//

class Main
{
  Void main()
  {
    ours := "his"
    boot := "hx.hxd.his"
    if (ours.compareTo(boot) >= 0)
      throw Err("'$ours' no longer sorts before boot historian '$boot' - " +
                "tdHis would lose Haxall's IHisExt tie-break and history " +
                "would silently fall back to Folio's in-memory historian, " +
                "which never persists his data to disk at all (Rec.hisUpdate " +
                "only sets an AtomicRef, hxFolio::StoreMgr is never called " +
                "for it) - every sample is lost on the next restart, clean " +
                "or not, on top of the hisMaxItems=1000 per-point cap while " +
                "it's up")
    echo("ok - '$ours' sorts before boot historian '$boot'")
  }
}
