#! /usr/bin/env fan
//
// fbfConn - real Haxall connector for FBF's BACnet/Modbus-to-MQTT bridge.
//

using build

class Build : BuildPod
{
  new make()
  {
    podName = "fbfConn"
    summary = "FBF bridge connector"
    meta    = ["org.name":     "Timberdoodle",
               "proj.name":    "Timberdoodle",
               "license.name": "Academic Free License 3.0",
               "vcs.name":     "Git"]
    depends  = ["sys @{fan.depend}",
                "util @{fan.depend}",
                "concurrent @{fan.depend}",
                "xeto @{hx.depend}",
                "haystack @{hx.depend}",
                "axon @{hx.depend}",
                "hx @{hx.depend}",
                "hxConn @{hx.depend}",
                "mqtt @{hx.depend}"]
    srcDirs = [`fan/`]
    resDirs = [`lib/`]
    index   = ["xeto.bindings": "fbf", "ph.lib": "fbf"]
  }
}
