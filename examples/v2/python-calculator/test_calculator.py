import pytest
from calculator import calculate


@pytest.mark.parametrize("left,op,right,expected", [(7,"+",5,12),(7,"-",5,2),(7,"*",5,35),(7,"/",2,3.5)])
def test_arithmetic(left, op, right, expected):
    assert calculate(left, op, right) == expected


@pytest.mark.parametrize("left,op,right", [(1,"/",0),(float("nan"),"+",1),(1,"?",2)])
def test_reject_invalid_input(left, op, right):
    with pytest.raises(ValueError):
        calculate(left, op, right)
