from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'templates' / 'index.html').read_text(encoding='utf-8')


def test_startup_community_qr_modal_contract():
    assert (ROOT / 'static' / 'images' / 'startup-community-qr.png').is_file()
    assert 'id="startupCommunityModal"' in HTML
    assert 'src="/static/images/startup-community-qr.png"' in HTML
    assert 'role="dialog" aria-modal="true"' in HTML
    assert 'data-startup-community-close' in HTML
    assert 'function initStartupCommunityModal()' in HTML
    assert 'initStartupCommunityModal();' in HTML
    assert "event.key==='Escape'" in HTML
    assert 'if(event.target===modal)close()' in HTML
    assert 'killPromo' not in HTML
    assert 'suppressPromoPopups' not in HTML


if __name__ == '__main__':
    test_startup_community_qr_modal_contract()
    print('startup community QR modal contract: PASS')
