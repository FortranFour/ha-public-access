"""Mirror mode's frontend glue.

The open integration keeps everything that makes the mirror *safe*: which
message types are forwarded at all, the read-only user, the entity and
statistic filters, the single published view, the panel list reduced to the
public dashboard, the owner's default dashboard hidden. It calls this module
only after those filters have run, so nothing here can widen what a visitor
reaches.

What lives here is what makes Home Assistant's own frontend *work* behind the
glass, and what has to follow the frontend release after release:

* the page scripts: a fake token, the websocket and REST requests steered to
  the public endpoint, the sidebar hidden, the header hidden or trimmed to its
  tab bar, editing dialogs blocked, navigation kept inside the dashboard;
* request rewrites: the dashboard is asked for by its public name and fetched
  under its real one;
* answer rewrites: the "lovelace" alias panel the frontend looks up while
  booting, the viewer presented as a plain, non-admin user;
* subscriptions to events a viewer may not see answered as subscriptions that
  never fire, instead of errors that become unhandled rejections in the page.

The integration checks API_VERSION before using this module.
"""

from __future__ import annotations

import json
import re
from typing import Any

API_VERSION = 1

HEX_COLOR = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})")


# -- the page --------------------------------------------------------------------


def head_script(public_path: str) -> str:
    """Runs before the frontend: fake credentials, and a websocket that comes here.

    The public page shares its origin with the owner's Home Assistant, and so
    its localStorage. Nothing may be written there: a fake token saved in it
    replaced the owner's real login, their Home Assistant then failed to
    authenticate again and again, and ip_ban locked the instance out (403 for
    everyone behind the reverse proxy). The fake token, the default panel and
    the hidden sidebar live only in this page's memory; the frontend reads and
    writes those keys through an in-memory shadow, never the real storage.
    Values left by older versions are removed on the first visit.
    """
    ws_path = f"/{public_path}/ws"
    panel = json.dumps(json.dumps(public_path))
    return (
        "<script>(function(){"
        "var origin=location.origin,S=Storage.prototype,g=S.getItem,s=S.setItem,r=S.removeItem;"
        # Clean what earlier versions persisted in the owner's storage.
        "try{var ls=window.localStorage,t=g.call(ls,'hassTokens');"
        "if(t&&(JSON.parse(t)||{}).access_token==='public')r.call(ls,'hassTokens');"
        f"if(g.call(ls,'defaultPanel')==={panel})r.call(ls,'defaultPanel');"
        "if(g.call(ls,'dockedSidebar')==='\"always_hidden\"')r.call(ls,'dockedSidebar');}catch(e){}"
        # The shadow: these keys never touch the real localStorage.
        "var mem={hassTokens:JSON.stringify({access_token:'public',token_type:'Bearer',"
        "expires_in:1800,hassUrl:origin,clientId:origin+'/',expires:Date.now()+315360000000,"
        f"refresh_token:'public'}}),dockedSidebar:'\"always_hidden\"',defaultPanel:{panel}}};"
        "function mine(o,k){try{return o===window.localStorage&&Object.prototype.hasOwnProperty.call(mem,k);}catch(e){return false;}}"
        "S.getItem=function(k){return mine(this,k)?mem[k]:g.call(this,k);};"
        "S.setItem=function(k,v){if(mine(this,k)){mem[k]=String(v);return;}return s.call(this,k,v);};"
        "S.removeItem=function(k){if(mine(this,k)){return;}return r.call(this,k);};"
        "var W=window.WebSocket;"
        "window.WebSocket=function(u,p){try{var x=new URL(u,origin);"
        f"if(x.pathname==='/api/websocket'){{x.pathname={json.dumps(ws_path)};u=x.toString();}}}}catch(e){{}}"
        "return p===undefined?new W(u):new W(u,p);};"
        "window.WebSocket.prototype=W.prototype;"
        "var F=window.fetch;"
        "window.fetch=function(i,o){var u=typeof i==='string'?i:(i&&i.url)||'';"
        "if(u.indexOf('/auth/token')>=0){return Promise.resolve(new Response("
        "JSON.stringify({access_token:'public',expires_in:1800,token_type:'Bearer'}),"
        "{status:200,headers:{'Content-Type':'application/json'}}));}"
        # Anything a card fetches from Home Assistant's REST API (history for
        # ApexCharts, a camera picture, a Frigate thumbnail, a calendar) goes to
        # the public endpoint instead, which answers only for what is
        # published. Sent to Home Assistant itself it would arrive with the
        # fake token and count as a failed login. A link that already carries
        # its own permission (token= or authSig=, map tiles) is left alone.
        "try{var h=new URL(u,origin);if(h.origin===origin&&h.pathname.indexOf('/api/')===0"
        # Map tiles carry their own token, in a header.
        "&&h.pathname.indexOf('/api/map_tiles/')!==0"
        "&&!h.searchParams.has('token')&&!h.searchParams.has('authSig')){"
        f"h.pathname={json.dumps('/' + public_path)}+h.pathname;"
        "var a=Array.prototype.slice.call(arguments);a[0]=typeof i==='string'?h.toString():new Request(h.toString(),i);"
        "return F.apply(this,a);}}catch(e){}"
        "return F.apply(this,arguments);};"
        # Only the published dashboard exists here. A card that navigates
        # anywhere else on this Home Assistant (settings, another dashboard)
        # would land on an error page, so such a move is simply not made.
        f"var base={json.dumps('/' + public_path)};"
        "function stay(u){try{if(u==null)return false;var x=new URL(u,location.href);"
        "return x.origin===origin&&x.pathname!==base&&x.pathname.indexOf(base+'/')!==0;}catch(e){return false;}}"
        "['pushState','replaceState'].forEach(function(n){var o=history[n];"
        "history[n]=function(d,t,u){if(stay(u))return;return o.apply(this,arguments);};});"
        "})();</script>"
    )


GLASS_SCRIPT = r"""
<script>(function(){
  // Cosmetic layer only: security is enforced server-side. Trims Home
  // Assistant's own chrome so the page is the dashboard and nothing else, and
  // blocks the edit and settings dialogs from ever opening.
  //
  // With one published view the whole header goes. With several, the header
  // stays as the tab bar (and the back arrow of a subview); only its menu
  // button and its edit/search/assistant buttons go.
  var TABS=__SHOW_HEADER__;
  var HEADER=TABS
    ? 'ha-menu-button,.action-items{display:none!important}'
      +'.toolbar{padding-inline-start:var(--ha-space-2,8px)!important}'
    : '.header,.toolbar{display:none!important}'
      +'#view{padding-top:0!important;margin-top:0!important}';
  var MAIN='ha-sidebar{display:none!important}';
  function deep(sel, root){root=root||document;var st=[root];while(st.length){var n=st.pop();
    var f=n.querySelector&&n.querySelector(sel);if(f)return f;
    var all=n.querySelectorAll?n.querySelectorAll('*'):[];for(var i=0;i<all.length;i++)if(all[i].shadowRoot)st.push(all[i].shadowRoot);}return null;}
  // A stylesheet inside the element's own shadow root: it keeps applying when
  // the frontend redraws the header (another tab, into a subview and back).
  function dress(host, css){var sh=host&&host.shadowRoot;if(!sh||sh.querySelector('style[data-public-access]'))return;
    var st=document.createElement('style');st.setAttribute('data-public-access','');st.textContent=css;sh.appendChild(st);}
  var root=null, main=null;
  function apply(search){
    if(!root||!root.isConnected)root=search?deep('hui-root'):null;
    if(!main||!main.isConnected)main=search?deep('home-assistant-main'):null;
    dress(root,HEADER);dress(main,MAIN);
  }
  // Look hard while the page builds, then keep an inexpensive watch: the
  // frontend makes a new dashboard root when the owner edits the dashboard.
  var n=0;var t=setInterval(function(){apply(true);if(++n>120){clearInterval(t);
    setInterval(function(){apply(!root||!root.isConnected||!main||!main.isConnected);},2000);}},250);
  // What goes wrong in the page is told to the owner (it shows in the
  // integration's diagnostics): a card that threw, a script that did not
  // load, an error card and what it says. A dozen at most per visit.
  var told={}, tellCount=0;
  function tell(kind, text){
    text=String(text||'').replace(/authSig=[\w.-]+/g,'authSig=...').slice(0,700);
    if(!text||told[text]||tellCount>=12)return; told[text]=1; tellCount++;
    var tries=0;(function go(){var ha=document.querySelector('home-assistant');var c=ha&&ha.hass&&ha.hass.connection;
      if(c&&c.sendMessagePromise){c.sendMessagePromise({type:'public_access/page_error',kind:kind,text:text,view:location.pathname}).catch(function(){});}
      else if(++tries<20)setTimeout(go,1500);})();
  }
  function stackOf(r){return r&&r.stack?String(r.stack).split('\n').slice(0,5).join(' | '):'';}
  window.addEventListener('error',function(e){
    var el=e.target;
    if(el&&el!==window&&el.tagName){if(el.tagName==='SCRIPT'||el.tagName==='LINK')tell('not loaded',el.src||el.href);return;}
    tell('error',(e.message||'')+' '+stackOf(e.error)+' @'+(e.filename||''));
  },true);
  window.addEventListener('unhandledrejection',function(e){var r=e.reason,t;
    if(r&&r.stack)t=stackOf(r);else{try{t=JSON.stringify(r);}catch(x){t=String(r);}}
    tell('rejection',t);});
  function deepAll(sel){var out=[],st=[document];while(st.length){var n=st.pop();if(!n.querySelectorAll)continue;
    var m=n.querySelectorAll(sel);for(var i=0;i<m.length;i++)out.push(m[i]);
    var all=n.querySelectorAll('*');for(var j=0;j<all.length;j++)if(all[j].shadowRoot)st.push(all[j].shadowRoot);}return out;}
  function errorCards(){deepAll('hui-error-card,hui-warning').slice(0,6).forEach(function(c){
    var t=((c.shadowRoot&&c.shadowRoot.textContent)||c.textContent||'').replace(/\s+/g,' ').trim();
    var cfg='';try{cfg=JSON.stringify(c._config||c.config||'').slice(0,200);}catch(x){}
    tell('error card',t+' '+cfg);});}
  var lastPath='';setInterval(function(){if(location.pathname!==lastPath){lastPath=location.pathname;setTimeout(errorCards,9000);}},1000);
  window.addEventListener('show-dialog',function(e){var d=e.detail&&e.detail.dialogTag||'';
    if(/edit|config|settings|search|quick-bar|voice|assist/i.test(d)){e.stopImmediatePropagation();}},true);
  document.addEventListener('keydown',function(e){if(e.key==='e'||e.key==='c'||e.key==='a'||e.key==='m')e.stopImmediatePropagation();},true);
})();</script>
"""


def glass_script(show_header: bool = False) -> str:
    """The cosmetic layer, with or without the tab bar."""
    return GLASS_SCRIPT.replace("__SHOW_HEADER__", "true" if show_header else "false")


def loading_style(color: str | None) -> str:
    """The launch screen in the owner's colour instead of the frontend's.

    Home Assistant paints its launch screen near-white (#fafafa) unless the
    visitor's device is in dark mode. On a wall screen or a digital signage
    player that flash shows on every reload. Only a validated hex colour is
    accepted, so nothing else can reach the page. On a dark colour the
    attribution switches to the variant the frontend uses in dark mode.
    """
    if not color or not HEX_COLOR.fullmatch(color):
        return ""
    digits = color[1:]
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    r, g, b = (int(digits[i : i + 2], 16) for i in (0, 2, 4))
    css = f"html:root,html:root body #ha-launch-screen{{background-color:{color}}}"
    if 0.2126 * r + 0.7152 * g + 0.0722 * b < 128:
        css += (
            "#ha-launch-screen{color:#e1e1e1}"
            "#ha-launch-screen .ohf-logo img{content:url(/static/images/open-home-foundation-on-dark.svg)}"
        )
    return f"<style>{css}</style>"


def assemble_page(
    index_html: str,
    public_path: str,
    loading_background: str | None = None,
    show_header: bool = False,
) -> str:
    """Home Assistant's own index page with the scripts put in place."""
    early = head_script(public_path) + loading_style(loading_background)
    glass = glass_script(show_header)
    head = index_html.find("<head>")
    if head < 0:
        return early + index_html + glass
    insert = head + len("<head>")
    return index_html[:insert] + early + index_html[insert:] + glass


# -- a visitor's connection --------------------------------------------------------


class Session:
    """Per-connection adaptation, used by the open integration's proxy."""

    def __init__(self, *, public_path: str, dashboard: str) -> None:
        self.public_path = public_path
        self.dashboard = dashboard
        self._silent: set[int] = set()

    def inbound(self, kind: str, msg: dict[str, Any]) -> dict[str, Any]:
        """Rewrite an allowed request before it is forwarded."""
        if kind == "lovelace/config":
            # The frontend asks for the dashboard by its public name.
            return {**msg, "url_path": self.dashboard}
        return msg

    def silent_subscription(self, msg_id: int) -> Any:
        """A subscription to events the viewer may not receive: acknowledged,
        and it simply never fires. Returns the result to send back."""
        self._silent.add(msg_id)
        return None

    def local_answer(self, kind: str, msg: dict[str, Any]) -> tuple[bool, Any]:
        """Answer here instead of forwarding. (answered, result)."""
        if kind == "unsubscribe_events" and msg.get("subscription") in self._silent:
            self._silent.discard(msg.get("subscription"))
            return True, None
        return False, None

    def dashboards(self, title: str | None = None) -> list[dict[str, Any]]:
        """The dashboard list as a visitor's cards see it: the published
        dashboard under its public name, and no other."""
        return [
            {
                "id": self.public_path,
                "url_path": self.public_path,
                "title": title or "Dashboard",
                "mode": "storage",
                "show_in_sidebar": False,
                "require_admin": False,
            }
        ]

    def outbound(self, kind: str, message: dict[str, Any]) -> dict[str, Any]:
        """Adapt an answer the integration has already filtered."""
        result = message.get("result")
        if message.get("type") != "result" or not isinstance(result, dict):
            return message
        if kind == "get_panels" and self.public_path in result:
            # The frontend also looks up its default panel by the fixed name
            # "lovelace" while booting and stops if it is missing: an alias of
            # the same published dashboard.
            alias = {**result[self.public_path], "url_path": "lovelace"}
            message["result"] = {**result, "lovelace": alias}
        elif kind == "auth/current_user":
            result["is_admin"] = False
            result["is_owner"] = False
        return message
