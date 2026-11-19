from monsieur_cuisine_mcp.web.recipe_pages import rewrite_image_host


def test_broken_user_content_host_is_rewritten_to_cdn_origin():
    url = "https://mc-web-user-content-cdn.tecpal.com/media-service-upload/images/1/image_details.jpg?x=1"
    assert rewrite_image_host(url) == "https://mc-recipe-user-prod.b-cdn.net/media-service-upload/images/1/image_details.jpg?x=1"


def test_other_hosts_untouched():
    for url in ["https://mc-web-content-cdn.tecpal.com/a.jpg", "https://cdn.example.test/b.jpg",
                "http://mc-web-user-content-cdn.tecpal.com/c.jpg"]:
        assert rewrite_image_host(url) == url
