from mealie.schema.openai.recipe_ingredient import OpenAIIngredients


def test_parse_openai_ingredients_wraps_root_array():
    result = OpenAIIngredients.parse_openai_response(
        '[{"quantity":"1/2","unit":"cup","food":"rice","note":null,"substitutes":[]}]'
    )

    assert result.ingredients[0].quantity == 0.5
    assert result.ingredients[0].food == "rice"
